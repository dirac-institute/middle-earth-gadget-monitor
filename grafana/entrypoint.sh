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
unset pw

exec /run.sh "$@"
