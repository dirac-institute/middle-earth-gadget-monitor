#!/bin/bash
# Restore local state from a shire backup. Run by init.sh with the stack
# stopped:  restore.sh latest|daily
#
# Expects /data (local state, empty vm/ and grafana/) and /shire (backups,
# read-only). Writes /data/.seeded on success, which allows backups to run.
set -euo pipefail

kind="${1:?usage: restore.sh latest|daily}"
src="/shire/$kind"

[ -f "$src/BACKUP_COMPLETE" ] || { echo "no complete backup at $src" >&2; exit 1; }
[ -z "$(find /data/vm /data/grafana -mindepth 1 -print -quit)" ] || { echo "/data/vm or /data/grafana is not empty" >&2; exit 1; }

vmrestore -loggerLevel=WARN -src="fs://$src/vm" -storageDataPath=/data/vm
if [ -f "$src/grafana.db" ]; then
    cp "$src/grafana.db" /data/grafana/grafana.db
fi
echo "restored $kind backup of $(cat "$src/BACKUP_COMPLETE") at $(date -u +%FT%TZ)" > /data/.seeded
cat /data/.seeded
