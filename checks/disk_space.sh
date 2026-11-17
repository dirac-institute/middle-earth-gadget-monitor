#!/bin/bash
# interval: 300
# timeout: 30
# Space on the hosts' local system disks: the root filesystem and /local
# (scratch and per-host data). stat -f on a local disk is a superblock read,
# cheap even when the host is struggling. A path that isn't its own local
# mount is skipped, so a missing /local isn't reported as a second copy of /.
# avail is what ordinary users can still write (blocks reserved for root
# don't count), so size - avail is what is used from their point of view.
for mnt in / /local; do
    awk -v m="$mnt" '$2 == m && $3 !~ /^nfs/ { f = 1 } END { exit !f }' /proc/mounts || continue
    read -r bs blocks avail < <(stat -f -c '%S %b %a' "$mnt")
    echo "node_filesystem_size_bytes{mountpoint=\"$mnt\"} $((bs * blocks))"
    echo "node_filesystem_avail_bytes{mountpoint=\"$mnt\"} $((bs * avail))"
done
