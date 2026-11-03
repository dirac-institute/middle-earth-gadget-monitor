#!/bin/bash
# Set up the rootless prototype (issue #6) beside the real, rootful stack.
# Run as root, from a checkout owned by the service account:
#
#   sudo -iu astro-gadget git clone https://github.com/dirac-institute/middle-earth-gadget-monitor.git gadget-monitor-rootless/repo
#   sudo ~astro-gadget/gadget-monitor-rootless/repo/rootless/setup.sh
#   sudo -iu astro-gadget
#   cd gadget-monitor-rootless/repo
#   podman compose -f compose.yaml -f rootless/compose.rootless.yaml up -d --build
#
# Grafana comes up on 127.0.0.1:18080. Nothing here touches the real stack:
# state lives next to the checkout (../local, ../shire), the database starts
# as a copy of the real latest backup (mounted read-only), and the backup
# container writes to ../shire/backup, not to /service/shire.
#
# Only two steps need root: a subordinate uid/gid range, so the images' own
# users can exist inside the user namespace, and lingering, so the user's
# systemd (and podman-restart) runs without a login and starts at boot.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROTO_DIR="$(dirname "$REPO_DIR")"
# shellcheck source=service.conf
source "$REPO_DIR/service.conf"

# Above every real uid and gid here (UW gids run to ~2.1e9), so the range can
# never alias a real account's files.
SUBID_START=3000000000
SUBID_COUNT=65536
REAL_BACKUP=/service/shire/gadget-monitor/backup
SSH_KEY=/service/shire/astro-gadget-ssh-key.key

[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }
[ "$(stat -c %u "$REPO_DIR")" = "$SVC_UID" ] || { echo "$REPO_DIR must be owned by $SVC_USER" >&2; exit 1; }

# A login session sets XDG_RUNTIME_DIR, which rootless podman and
# systemctl --user need.
as_svc() { sudo -iu "$SVC_USER" "$@"; }

echo "== Subordinate ids =="
if grep -q "^$SVC_USER:" /etc/subuid && grep -q "^$SVC_USER:" /etc/subgid; then
    echo "  ok   $(grep "^$SVC_USER:" /etc/subuid)"
else
    end=$((SUBID_START + SUBID_COUNT - 1))
    usermod --add-subuids "$SUBID_START-$end" --add-subgids "$SUBID_START-$end" "$SVC_USER"
    echo "  added $SUBID_START-$end to /etc/subuid and /etc/subgid"
fi

echo "== Lingering =="
loginctl enable-linger "$SVC_USER"
for _ in $(seq 20); do [ -d "/run/user/$SVC_UID" ] && break; sleep 0.5; done
echo "  ok   user manager running for $SVC_USER"

echo "== Restart at boot =="
# The user-level twin of the system unit init.sh enables.
as_svc systemctl --user enable podman-restart.service >/dev/null 2>&1
echo "  ok   podman-restart.service enabled for $SVC_USER"

echo "== Prototype state ($PROTO_DIR) =="
as_svc mkdir -p "$PROTO_DIR/local/vm" "$PROTO_DIR/local/grafana" \
                "$PROTO_DIR/shire/backup" "$PROTO_DIR/shire/secrets"
as_svc chmod 0700 "$PROTO_DIR/shire/secrets"
PW_FILE="$PROTO_DIR/shire/secrets/grafana-admin-password"
as_svc sh -c 'umask 077; [ -s "$1" ] || head -c 24 /dev/urandom | base64 > "$1"' _ "$PW_FILE"
echo "  ok   admin password in $PW_FILE"

as_svc sh -c 'cat > "$1"' _ "$REPO_DIR/.env" <<EOF
SVC_USER=$SVC_USER
SVC_UID=$SVC_UID
SVC_GID=$SVC_GID
SVC_FQDN=$(hostname -f)
GADGET_LOCAL_DIR=$PROTO_DIR/local
GADGET_SHIRE_DIR=$PROTO_DIR/shire
GADGET_SSH_KEY=$SSH_KEY
EOF
echo "  ok   wrote $REPO_DIR/.env"

echo "== Seed from the real backup (read-only) =="
if as_svc test -e "$PROTO_DIR/local/.seeded"; then
    echo "  ok   already seeded ($(cat "$PROTO_DIR/local/.seeded"))"
else
    as_svc podman build -q -t localhost/gadget-monitor/backup:latest "$REPO_DIR/backup" >/dev/null
    as_svc podman run --rm --userns=keep-id --user "$SVC_UID:$SVC_GID" \
        -v "$PROTO_DIR/local:/data:z" -v "$REAL_BACKUP:/shire:ro" \
        localhost/gadget-monitor/backup:latest /usr/local/bin/restore.sh latest
fi

echo
echo "Next, as $SVC_USER (sudo -iu $SVC_USER):"
echo "  cd $REPO_DIR && podman compose -f compose.yaml -f rootless/compose.rootless.yaml up -d --build"
echo "Grafana: http://127.0.0.1:18080/"
