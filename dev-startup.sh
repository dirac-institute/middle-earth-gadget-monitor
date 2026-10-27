#!/bin/bash
# Run the stack locally for development (macOS/Linux with Docker).
#
#   ./dev-startup.sh
#
# State lives under ./dev-data instead of /local and /service/shire. The
# containers run as your own uid. To reach the real hosts, copy the service's
# SSH key to dev-data/ssh-key (mode 0600); without it the collector simply
# reports both hosts as unreachable, which is fine for dashboard and backup
# work.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF_FILE="$REPO_DIR/service.conf"
DEV="$REPO_DIR/dev-data"

if [ ! -f "$CONF_FILE" ]; then
    echo "FATAL: $CONF_FILE not found." >&2
    exit 1
fi
# shellcheck source=service.conf
source "$CONF_FILE"

mkdir -p "$DEV/local/vm" "$DEV/local/grafana" "$DEV/shire/backup" "$DEV/shire/secrets"
[ -f "$DEV/local/.seeded" ] || echo "dev $(date -u +%FT%TZ)" > "$DEV/local/.seeded"
if [ ! -s "$DEV/shire/secrets/grafana-admin-password" ]; then
    (umask 077; echo admin > "$DEV/shire/secrets/grafana-admin-password")
fi
[ -e "$DEV/ssh-key" ] || (umask 077; : > "$DEV/ssh-key")

export SVC_USER="$SVC_USER"
export SVC_UID="$(id -u)"
export SVC_GID="$(id -g)"
export SVC_FQDN=localhost
export GADGET_LOCAL_DIR="$DEV/local"
export GADGET_SHIRE_DIR="$DEV/shire"
export GADGET_SSH_KEY="$DEV/ssh-key"

docker compose up -d --build --force-recreate
echo
echo "Running at http://localhost:8080/  (admin password: admin)"
echo "Stop with: docker compose down"
