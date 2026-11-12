#!/bin/bash
# interval: 300
# timeout: 30
# hosts: shire
# The storage behind each NFS export, kept apart because they perform very
# differently: shire (/data, one 1.4 PB volume behind a MegaRAID controller)
# and shiren (/nvme, md RAID0 over two NVMe drives). All world-readable:
#   /proc/diskstats                  I/O counts, bytes and time per device
#   /sys/fs/xfs/<dev>/stats/stats    XFS counters for that filesystem
#   stat -f                          space and inodes (local disks, cheap)
# Devices are found from the mounts. level is "volume" for the device the
# filesystem sits on, "member" for the drives under an md array (busy % and
# wait per I/O are only meaningful on those, not on md itself).
#
# XFS: log forces are waits for the journal to reach disk (fsync and NFS
# COMMIT); force sleeps are the ones that had to block; log space waits are
# transactions that stalled because the journal was full. Inode cache misses
# are inode reads that had to go to disk.
#
# The kernel keeps the diskstats times as 32-bit milliseconds, so they wrap
# about every 50 days of busy time; Grafana sees a counter reset and loses
# one interval.
for m in /data:shire /nvme:shiren; do
    mnt=${m%%:*}; fs=${m##*:}
    src=$(awk -v m="$mnt" '$2 == m { print $1; exit }' /proc/mounts)
    [ -n "$src" ] || continue
    dev=${src#/dev/}
    echo "map $dev $fs volume"
    for s in /sys/block/"$dev"/slaves/*; do
        [ -e "$s" ] && echo "map ${s##*/} $fs member"
    done
    if [ -r "/sys/fs/xfs/$dev/stats/stats" ]; then
        sed "s/^/xfs $fs /" "/sys/fs/xfs/$dev/stats/stats"
    fi
    # stat -f: block size, total, available blocks; total and free inodes.
    read -r bs blocks avail files ffree < <(stat -f -c '%S %b %a %c %d' "$mnt")
    echo "space $fs $((bs * blocks)) $((bs * avail)) $files $ffree"
done | awk '
FNR == NR && $1 == "map" { fs[$2] = $3; level[$2] = $4; next }
FNR == NR && $1 == "xfs" {
    f = $2
    if ($3 == "log")      { x(f, "log_forces", $7); x(f, "log_force_sleeps", $8); x(f, "log_writes", $4) }
    if ($3 == "push_ail") { x(f, "log_space_waits", $5) }
    if ($3 == "ig")       { x(f, "inode_cache_misses", $7) }
    if ($3 == "rw")       { x(f, "write_calls", $4); x(f, "read_calls", $5) }
    next
}
FNR == NR && $1 == "space" {
    l = sprintf("{fs=\"%s\"}", $2)
    printf "node_filesystem_size_bytes%s %s\n", l, $3
    printf "node_filesystem_avail_bytes%s %s\n", l, $4
    printf "node_filesystem_files%s %s\n", l, $5
    printf "node_filesystem_files_free%s %s\n", l, $6
    next
}
function x(f, n, v) { printf "gadget_xfs_%s_total{fs=\"%s\"} %s\n", n, f, v }
# /proc/diskstats: 4 reads, 6 sectors read, 7 ms reading, 8 writes,
# 10 sectors written, 11 ms writing, 12 in flight, 13 ms busy, 14 weighted ms.
FNR != NR && ($3 in fs) {
    l = sprintf("{device=\"%s\",fs=\"%s\",level=\"%s\"}", $3, fs[$3], level[$3])
    printf "node_disk_reads_completed_total%s %s\n", l, $4
    printf "node_disk_read_bytes_total%s %.0f\n", l, $6 * 512
    printf "node_disk_read_time_seconds_total%s %.3f\n", l, $7 / 1000
    printf "node_disk_writes_completed_total%s %s\n", l, $8
    printf "node_disk_written_bytes_total%s %.0f\n", l, $10 * 512
    printf "node_disk_write_time_seconds_total%s %.3f\n", l, $11 / 1000
    printf "node_disk_io_now%s %s\n", l, $12
    printf "node_disk_io_time_seconds_total%s %.3f\n", l, $13 / 1000
    printf "node_disk_io_time_weighted_seconds_total%s %.3f\n", l, $14 / 1000
}' - /proc/diskstats
