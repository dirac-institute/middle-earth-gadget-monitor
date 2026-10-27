#!/bin/bash
# One backup run: local state in /data -> /shire/latest (and /shire/daily once
# a day). Sourced with --lib, it only defines the helper functions used by
# backup-loop.sh.
#
# Layout written on shire (each copy is self-contained):
#   <kind>/vm/                 vmbackup output, updated incrementally
#   <kind>/grafana.db          consistent SQLite copy
#   <kind>/BACKUP_COMPLETE     written last; removed before a copy starts
set -uo pipefail

VM_URL="${GADGET_VM_URL:?GADGET_VM_URL must be set}"
STATE=/data/.backup-state
# Hard cap from the 10 GB shire budget: latest + daily <= 9 GB plus grafana.db.
LOCAL_LIMIT_BYTES="${LOCAL_LIMIT_BYTES:-4831838208}"   # 4.5 GiB
DAILY_EVERY_S=$((24 * 3600 - 600))

log() { echo "$(date -u +%FT%TZ) backup: $*" >&2; }

state_get() { [ -f "$STATE" ] && sed -n "s/^$1=//p" "$STATE" | tail -1; }
state_set() {
    local tmp="$STATE.tmp"
    { [ -f "$STATE" ] && grep -v "^$1=" "$STATE"; echo "$1=$2"; } > "$tmp"
    mv -f "$tmp" "$STATE"
}

push_metrics() {
    curl -fsS --max-time 10 --data-binary @- "$VM_URL/api/v1/import/prometheus" \
        || log "could not push metrics to $VM_URL"
}

# Emitted every 5 minutes so dashboards always have a recent value.
report_status() {
    local vm_bytes
    vm_bytes="$(du -sb /data/vm 2>/dev/null | cut -f1)"
    {
        echo "gadget_storage_bytes{location=\"local_vm\"} ${vm_bytes:-0}"
        echo "gadget_storage_limit_bytes{location=\"local_vm\"} $LOCAL_LIMIT_BYTES"
        local v
        v="$(state_get shire_bytes)";             [ -n "$v" ] && echo "gadget_storage_bytes{location=\"shire_backup\"} $v"
        v="$(state_get last_success_latest)";     [ -n "$v" ] && echo "gadget_backup_last_success_timestamp_seconds{kind=\"latest\"} $v"
        v="$(state_get last_success_daily)";      [ -n "$v" ] && echo "gadget_backup_last_success_timestamp_seconds{kind=\"daily\"} $v"
        v="$(state_get last_duration_s)";         [ -n "$v" ] && echo "gadget_backup_duration_seconds $v"
        for r in ok failed refused_unseeded refused_oversize; do
            [ "$(state_get last_result)" = "$r" ] && echo "gadget_backup_last_result{result=\"$r\"} 1" \
                                                  || echo "gadget_backup_last_result{result=\"$r\"} 0"
        done
    } | push_metrics
}

backup_to() {
    local kind="$1" dst="/shire/$1"
    log "starting $kind"
    mkdir -p "$dst" || return 1
    rm -f "$dst/BACKUP_COMPLETE" || return 1
    vmbackup -loggerLevel=WARN \
        -storageDataPath=/data/vm \
        -snapshot.createURL="$VM_URL/snapshot/create" \
        -dst="fs://$dst/vm" || return 1
    if [ -f /data/grafana/grafana.db ]; then
        # .backup gives a consistent copy while Grafana is running. Write it
        # on local tmp first so SQLite never takes locks on NFS.
        rm -f /tmp/grafana.db
        sqlite3 /data/grafana/grafana.db ".backup '/tmp/grafana.db'" || return 1
        cp /tmp/grafana.db "$dst/grafana.db.tmp" && mv -f "$dst/grafana.db.tmp" "$dst/grafana.db" || return 1
        rm -f /tmp/grafana.db
    fi
    date -u +%FT%TZ > "$dst/BACKUP_COMPLETE" || return 1
    state_set "last_success_$kind" "$(date +%s)"
    log "finished $kind"
}

main() {
    if [ ! -e /data/.seeded ]; then
        log "REFUSED: /data/.seeded missing; run init.sh (restore or --fresh) first"
        state_set last_result refused_unseeded
        return 1
    fi
    local size
    size="$(du -sb /data/vm | cut -f1)"
    if [ "$size" -gt "$LOCAL_LIMIT_BYTES" ]; then
        log "REFUSED: local database is $size bytes, over the $LOCAL_LIMIT_BYTES limit"
        state_set last_result refused_oversize
        return 1
    fi

    local start rc=0
    start="$(date +%s)"
    backup_to latest || rc=1
    if [ "$rc" -eq 0 ]; then
        local last_daily
        last_daily="$(state_get last_success_daily)"
        if [ -z "$last_daily" ] || [ $(( $(date +%s) - last_daily )) -ge "$DAILY_EVERY_S" ]; then
            backup_to daily || rc=1
        fi
    fi
    state_set last_duration_s $(( $(date +%s) - start ))
    state_set shire_bytes "$(du -sb /shire | cut -f1)"
    if [ "$rc" -eq 0 ]; then state_set last_result ok; else state_set last_result failed; log "FAILED"; fi
    return "$rc"
}

if [ "${1:-}" != "--lib" ]; then
    main
    rc=$?
    report_status
    exit "$rc"
fi
