#!/bin/bash
# Make the admin password file on shire authoritative, turn on UW NetID sign-in
# when its client credentials are on shire, and set folder permissions.
#
# Grafana only reads GF_SECURITY_ADMIN_PASSWORD when it first creates the
# admin user, so on an existing (or restored) grafana.db we reset the password
# from the file before starting. Files on shire are read with a timeout so a
# hung shire delays startup by seconds, not forever.
set -euo pipefail

SECRETS=/run/secrets/grafana
PW_FILE=$SECRETS/grafana-admin-password
DB="$GF_PATHS_DATA/grafana.db"

if pw="$(timeout -k 5 20 cat "$PW_FILE" 2>/dev/null)" && [ -n "$pw" ]; then
    export GF_SECURITY_ADMIN_PASSWORD="$pw"
    if [ -s "$DB" ]; then
        grafana cli --homepath "$GF_PATHS_HOME" --config "$GF_PATHS_CONFIG" \
            admin reset-admin-password --password-from-stdin <<<"$pw" >/dev/null \
            && echo "gadget: admin password synced from $PW_FILE" \
            || echo "gadget: WARNING could not reset admin password" >&2
    fi
else
    echo "gadget: WARNING $PW_FILE unreadable; admin password left unchanged" >&2
fi

# UW NetID sign-in (OpenID Connect against the UW IdP; the rest of its
# settings are in compose.yaml). Off until UW-IT has registered the client and
# both files exist, so a deploy never shows a sign-in button that can't work.
if id="$(timeout -k 5 20 cat "$SECRETS/uw-oidc-client-id" 2>/dev/null)" && [ -n "$id" ] &&
   secret="$(timeout -k 5 20 cat "$SECRETS/uw-oidc-client-secret" 2>/dev/null)" && [ -n "$secret" ]; then
    export GF_AUTH_GENERIC_OAUTH_ENABLED=true
    export GF_AUTH_GENERIC_OAUTH_CLIENT_ID="$id"
    export GF_AUTH_GENERIC_OAUTH_CLIENT_SECRET="$secret"
    echo "gadget: UW NetID sign-in enabled"
else
    echo "gadget: UW NetID sign-in off (no client credentials in $SECRETS)"
fi
unset id secret

# Folder permissions. File provisioning can't set them, so once Grafana
# answers:
# - Admin (per-user usage): remove every grant (org admins always keep access).
# - UW NetID: View for the "UW NetID" team only.
# then confirm an anonymous request is refused for each. The permissions
# persist in grafana.db; this re-applies them on every start in case someone
# edited them in the UI.
#
# Adding users to teams at sign-in (team sync) is a Grafana Enterprise
# feature, so instead this keeps every user other than the admin in the team,
# checking every 10 seconds. With sign-up off except through UW NetID, those
# are the NetID users. A first-time user may wait that long for the folder.
api=http://127.0.0.1:${GF_SERVER_HTTP_PORT:-3000}/api
call() {
    curl -sf -K - -H 'Content-Type: application/json' "$@" \
        <<<"user = \"$GF_SECURITY_ADMIN_USER:$pw\""
}
check_anonymous() {  # dashboard uid, folder name
    local code
    code=$(curl -s -o /dev/null -w '%{http_code}' "$api/dashboards/uid/$1")
    if [ "$code" = 200 ]; then
        echo "gadget: WARNING $2 folder is still visible anonymously" >&2
    else
        echo "gadget: $2 folder restricted (anonymous gets $code)"
    fi
}
setup_access() {
    local team='' members users uid
    for _ in $(seq 60); do
        sleep 5
        call -o /dev/null -X POST -d '{"items":[]}' "$api/folders/gadget-admin/permissions" || continue
        team=$(call "$api/teams/search?name=UW%20NetID" | jq -r '.teams[0].id // empty') || continue
        if [ -z "$team" ]; then
            team=$(call -X POST -d '{"name":"UW NetID"}' "$api/teams" | jq -r '.teamId // empty') || continue
        fi
        [ -n "$team" ] || continue
        call -o /dev/null -X POST -d "{\"items\":[{\"teamId\":$team,\"permission\":1}]}" \
            "$api/folders/gadget-uw/permissions" || { team=''; continue; }
        break
    done
    check_anonymous gadget-users Admin
    check_anonymous gadget-uw-status "UW NetID"
    if [ -z "$team" ]; then
        echo "gadget: WARNING could not set folder permissions; restricted dashboards may be public" >&2
        return
    fi
    while sleep 10; do
        members=$(call "$api/teams/$team/members" | jq -r '.[].userId') || continue
        users=$(call "$api/org/users" |
            jq -r --arg admin "$GF_SECURITY_ADMIN_USER" '.[] | select(.login != $admin) | .userId') || continue
        for uid in $(comm -13 <(sort <<<"$members") <(sort <<<"$users")); do
            call -o /dev/null -X POST -d "{\"userId\":$uid}" "$api/teams/$team/members" &&
                echo "gadget: added user $uid to the UW NetID team"
        done
    done
}
if [ -n "${pw:-}" ]; then
    setup_access &
fi
unset pw

exec /run.sh "$@"
