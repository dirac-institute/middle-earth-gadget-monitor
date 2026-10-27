#!/bin/bash
# Backup scheduler. Every 5 minutes it reports backup status metrics; once an
# hour (at minute BACKUP_MINUTE) it runs backup-once.sh.
#
# backup-once.sh is run under flock -n (a still-running backup means this one
# is skipped) and timeout, so a hung NFS share stalls only this container and
# never piles up work.
set -uo pipefail

BACKUP_MINUTE="${BACKUP_MINUTE:-7}"
STATE=/data/.backup-state
LOCK=/tmp/backup.lock

source /usr/local/bin/backup-once.sh --lib

last_hour_run=""
while true; do
    hour="$(date -u +%Y%m%d%H)"
    minute="$((10#$(date -u +%M)))"
    if [ "$minute" -ge "$BACKUP_MINUTE" ] && [ "$hour" != "$last_hour_run" ]; then
        last_hour_run="$hour"
        flock -n "$LOCK" timeout -k 30 45m /usr/local/bin/backup-once.sh \
            || log "backup run exited non-zero (see above)"
    fi
    report_status
    # Sleep to the next 5-minute boundary.
    sleep "$(( 300 - $(date +%s) % 300 ))"
done
