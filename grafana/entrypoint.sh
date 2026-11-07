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

# Folder permissions and the UW NetID team are set through the HTTP API once
# Grafana answers, so they run in the background (see access.py).
if [ -n "${pw:-}" ]; then
    python3 -I -u /usr/local/bin/gadget-access.py <<<"$pw" &
fi
unset pw

exec /run.sh "$@"
