#!/bin/bash
# Host initialization for the gadget-monitor VM. Run as root; safe to re-run.
#
#   git clone <this repo> && cd middle-earth-gadget-monitor
#   sudo ./init.sh                      # restores local state from shire if needed
#   podman compose up -d --build
#
# Options (only consulted when /local state has not been seeded yet):
#   --fresh                  start with an empty database (refused if a backup exists)
#   --restore-from=latest    restore from the hourly backup (default)
#   --restore-from=daily     restore from the daily backup
#
# Only host-level concerns live here (packages, service account, SSH
# hardening, directories, seeding local state). Everything else belongs in the
# container images.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$REPO_DIR/.env"
CONF_FILE="$REPO_DIR/service.conf"

# Fixed deployment paths. Live state is on local disk; shire holds backups and
# secrets. See PLAN.md for why the database is not on NFS.
LOCAL_DIR=/local/gadget-monitor
SHIRE_DIR=/service/shire/gadget-monitor
SSH_KEY=/service/shire/astro-gadget-ssh-key.key
BACKUP_IMAGE=localhost/gadget-monitor/backup:latest

FRESH=0
RESTORE_FROM=latest
for arg in "$@"; do
    case "$arg" in
        --fresh)               FRESH=1 ;;
        --restore-from=latest) RESTORE_FROM=latest ;;
        --restore-from=daily)  RESTORE_FROM=daily ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

if [ ! -f "$CONF_FILE" ]; then
    echo "FATAL: $CONF_FILE not found." >&2
    echo "This file defines SVC_USER, SVC_UID, and SVC_GID for the service account." >&2
    exit 1
fi
# shellcheck source=service.conf
source "$CONF_FILE"
for var in SVC_USER SVC_UID SVC_GID; do
    if [ -z "${!var:-}" ]; then
        echo "FATAL: $var is not set in $CONF_FILE" >&2
        exit 1
    fi
done

WARNINGS=0
warn() { echo "WARNING: $*" >&2; WARNINGS=$((WARNINGS + 1)); }
fatal() { echo "FATAL: $*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }

# Run a command as the service account with a timeout. /service/shire is
# exported with root_squash, so root cannot read it, and it is hard-mounted,
# so an unbounded call could hang forever.
as_svc() {
    local secs="$1"; shift
    timeout -k 5 "$secs" setpriv --reuid="$SVC_UID" --regid="$SVC_GID" --clear-groups "$@"
}

# ── Packages ────────────────────────────────────────────────────────────────
echo "== Packages =="
need=()
command -v podman >/dev/null || need+=(podman)
command -v podman-compose >/dev/null || need+=(podman-compose)
if [ ${#need[@]} -gt 0 ]; then
    rpm -q epel-release >/dev/null 2>&1 || dnf install -y epel-release
    dnf install -y "${need[@]}"
else
    echo "podman and podman-compose already installed"
fi
podman --version

# A compose restart policy only survives reboot if this unit is enabled.
systemctl enable --now podman-restart.service >/dev/null 2>&1 \
    && echo "podman-restart.service enabled" \
    || warn "could not enable podman-restart.service (containers won't restart at boot)"

# ── SSH hardening ───────────────────────────────────────────────────────────
echo
echo "== SSH hardening =="
SSHD_CONF=/etc/ssh/sshd_config.d/00-svc-hardening.conf
cat > "$SSHD_CONF" <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
EOF
chmod 0600 "$SSHD_CONF"
if sshd -t >/dev/null 2>&1; then
    systemctl reload sshd >/dev/null 2>&1 \
        && echo "  ok   password authentication disabled" \
        || warn "could not reload sshd"
else
    warn "sshd config validation failed; check $SSHD_CONF"
fi

# ── Service account ────────────────────────────────────────────────────────
echo
echo "== Service account =="
# The containers run as this uid. Secrets and backups live on a shared NFS
# mount exported with root_squash, meaning root cannot read mode-0600 files --
# only the service account can. See SECRETS.md.
if getent group "$SVC_GID" >/dev/null; then
    have_group="$(getent group "$SVC_GID" | cut -d: -f1)"
    if [ "$have_group" = "$SVC_USER" ]; then
        echo "  ok   group $SVC_USER ($SVC_GID) exists"
    else
        warn "gid $SVC_GID belongs to group '$have_group', expected '$SVC_USER'"
    fi
else
    groupadd -g "$SVC_GID" "$SVC_USER"
    echo "  added group $SVC_USER ($SVC_GID)"
fi

if getent passwd "$SVC_UID" >/dev/null; then
    have_user="$(getent passwd "$SVC_UID" | cut -d: -f1)"
    if [ "$have_user" = "$SVC_USER" ]; then
        echo "  ok   user $SVC_USER ($SVC_UID) exists"
    else
        warn "uid $SVC_UID belongs to user '$have_user', expected '$SVC_USER'"
    fi
else
    useradd -u "$SVC_UID" -g "$SVC_GID" -M -d /nonexistent -s /bin/bash \
            -c "Service account" "$SVC_USER" 2>/dev/null
    echo "  added user $SVC_USER ($SVC_UID)"
fi

# ── Local state directories ────────────────────────────────────────────────
echo
echo "== Local state ($LOCAL_DIR) =="
install -d -m 0750 -o "$SVC_UID" -g "$SVC_GID" "$LOCAL_DIR" "$LOCAL_DIR/vm" "$LOCAL_DIR/grafana"
echo "  ok   $LOCAL_DIR/{vm,grafana} owned by $SVC_USER"

# ── Shire: backups and secrets ─────────────────────────────────────────────
echo
echo "== Shire ($SHIRE_DIR) =="
if ! as_svc 60 test -d /service/shire; then
    fatal "/service/shire is not reachable as $SVC_USER (missing, hung, or permission denied)"
fi
as_svc 60 mkdir -p "$SHIRE_DIR/backup" "$SHIRE_DIR/secrets"
as_svc 60 chmod 0700 "$SHIRE_DIR/secrets"

PW_FILE="$SHIRE_DIR/secrets/grafana-admin-password"
if as_svc 60 test -s "$PW_FILE"; then
    echo "  ok   Grafana admin password present"
else
    # Generated as the service account so root never holds it on disk.
    as_svc 60 sh -c 'umask 077; head -c 24 /dev/urandom | base64 > "$1"' _ "$PW_FILE"
    echo "  added Grafana admin password: read it with"
    echo "         sudo -u $SVC_USER cat $PW_FILE"
fi

if as_svc 60 test -r "$SSH_KEY"; then
    echo "  ok   SSH key $SSH_KEY readable by $SVC_USER"
else
    warn "SSH key $SSH_KEY is not readable by $SVC_USER; the collector cannot log in"
fi

# ── Compose environment ────────────────────────────────────────────────────
echo
echo "== Compose environment =="
FQDN="$(hostname -f 2>/dev/null || hostname)"
case "$FQDN" in
    *.*) ;;
    *)   warn "hostname '$FQDN' is not fully qualified; check DNS and /etc/hosts" ;;
esac
# .env is gitignored and per-host; service.conf is the committed source of
# truth for the identity values.
cat > "$ENV_FILE" <<EOF
SVC_USER=$SVC_USER
SVC_UID=$SVC_UID
SVC_GID=$SVC_GID
SVC_FQDN=$FQDN
GADGET_LOCAL_DIR=$LOCAL_DIR
GADGET_SHIRE_DIR=$SHIRE_DIR
GADGET_SSH_KEY=$SSH_KEY
EOF
echo "  ok   wrote $ENV_FILE (SVC_FQDN=$FQDN)"

# ── Seed local state ───────────────────────────────────────────────────────
# The backup container refuses to run until $LOCAL_DIR/.seeded exists. That
# marker is written only here: by a restore, or by an explicit --fresh. This
# stops an empty database on a new VM from overwriting a good backup.
echo
echo "== Seed local state =="
is_empty() { [ -z "$(ls -A "$1")" ]; }

if [ -e "$LOCAL_DIR/.seeded" ]; then
    echo "  ok   already seeded ($(cat "$LOCAL_DIR/.seeded"))"
    if [ "$FRESH" -eq 1 ] || [ "$RESTORE_FROM" != latest ]; then
        warn "--fresh/--restore-from ignored: local state is already seeded"
    fi
elif ! is_empty "$LOCAL_DIR/vm" || ! is_empty "$LOCAL_DIR/grafana"; then
    fatal "$LOCAL_DIR has data but no .seeded marker. Inspect it, then move it aside and re-run."
else
    SRC="$SHIRE_DIR/backup/$RESTORE_FROM"
    if as_svc 60 test -f "$SRC/BACKUP_COMPLETE"; then
        if podman ps --format '{{.Names}}' | grep -q '^gadget-monitor-'; then
            fatal "gadget-monitor containers are running; 'podman compose down' before restoring"
        fi
        [ "$FRESH" -eq 0 ] || fatal "--fresh refused: a backup exists at $SRC; restoring it instead is the safe choice"
        echo "  restoring from $SRC ($(as_svc 60 cat "$SRC/BACKUP_COMPLETE"))"
        podman build -q -t "$BACKUP_IMAGE" "$REPO_DIR/backup" >/dev/null
        podman run --rm --user "$SVC_UID:$SVC_GID" \
            -v "$LOCAL_DIR:/data:z" -v "$SHIRE_DIR/backup:/shire:ro" \
            "$BACKUP_IMAGE" /usr/local/bin/restore.sh "$RESTORE_FROM"
        echo "  ok   restored"
    elif as_svc 60 sh -c 'ls -A "$1"/* 2>/dev/null | grep -q .' _ "$SHIRE_DIR/backup"; then
        fatal "no complete backup at $SRC, but $SHIRE_DIR/backup is not empty. Try --restore-from=daily, or inspect it."
    elif [ "$FRESH" -eq 1 ]; then
        echo "fresh $(date -u +%FT%TZ)" > "$LOCAL_DIR/.seeded"
        chown "$SVC_UID:$SVC_GID" "$LOCAL_DIR/.seeded"
        echo "  ok   seeded fresh (empty database)"
    else
        fatal "no backup found at $SHIRE_DIR/backup. If this is the first deployment, re-run with --fresh."
    fi
fi

echo
if [ "$WARNINGS" -gt 0 ]; then
    echo "init complete with $WARNINGS warning(s)."
else
    echo "init complete."
fi
echo "Next: podman compose up -d --build"
