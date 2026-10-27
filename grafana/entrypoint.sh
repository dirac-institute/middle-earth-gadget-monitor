#!/bin/bash
# Make the admin password file on shire authoritative.
#
# Grafana only reads GF_SECURITY_ADMIN_PASSWORD when it first creates the
# admin user, so on an existing (or restored) grafana.db we reset the password
# from the file before starting. The file is read with a timeout so a hung
# shire delays startup by seconds, not forever.
set -euo pipefail

PW_FILE=/run/secrets/grafana/grafana-admin-password
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

# Make the provisioned "Admin" folder (per-user usage) admin-only. File
# provisioning can't set folder permissions, so once Grafana answers, remove
# every role's grant on it (org admins always keep access), then confirm an
# anonymous request is refused. The permissions persist in grafana.db; this
# re-applies them on every start in case someone edited them in the UI.
lock_admin_folder() {
    local api=http://127.0.0.1:${GF_SERVER_HTTP_PORT:-3000}/api i code
    for i in $(seq 60); do
        sleep 5
        curl -sf -o /dev/null -K - -X POST -H 'Content-Type: application/json' \
            -d '{"items":[]}' "$api/folders/gadget-admin/permissions" \
            <<<"user = \"$GF_SECURITY_ADMIN_USER:$pw\"" || continue
        code=$(curl -s -o /dev/null -w '%{http_code}' "$api/dashboards/uid/gadget-users")
        if [ "$code" = 200 ]; then
            echo "gadget: WARNING Admin folder is still visible anonymously" >&2
        else
            echo "gadget: Admin folder restricted to admins (anonymous gets $code)"
        fi
        return
    done
    echo "gadget: WARNING could not restrict the Admin folder; per-user dashboard may be public" >&2
}
if [ -n "${pw:-}" ]; then
    lock_admin_folder &
fi
unset pw

exec /run.sh "$@"
