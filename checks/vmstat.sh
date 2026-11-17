#!/bin/bash
# interval: 300
# timeout: 30
# Memory reclaim pressure from /proc/vmstat (cumulative since boot):
#   allocstall_*         times an allocation had to stop and reclaim memory
#                        itself (direct reclaim), per memory zone
#   pgscan/pgsteal_*     pages scanned and reclaimed, by kswapd (background)
#                        and directly by allocating processes
#   oom_kill             processes killed by the out-of-memory killer
# Direct reclaim stalls whatever process is allocating, so a rising
# allocstall rate means memory pressure is slowing work down, even while
# MemAvailable looks healthy.
awk '
$1 ~ /^allocstall_/ || $1 ~ /^(pgscan|pgsteal)_(direct|kswapd)$/ || $1 == "oom_kill" {
    printf "node_vmstat_%s %s\n", $1, $2
}' /proc/vmstat
