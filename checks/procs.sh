#!/bin/bash
# interval: 60
# timeout: 30
# Processes by state, and NFSv4 state manager threads, from one ps call.
#
# Processes stuck in D (uninterruptible sleep) were the first visible symptom
# of the November 2026 NFS outage: jupyter kernels hung on NFS calls. ps only
# reads /proc/<pid>/stat here, which never waits on NFS or on a process's
# memory lock, so it is safe on a struggling host. Threads are not counted.
#
# The NFSv4 client starts a kernel thread named "<server address>-manager"
# (truncated to 15 characters) to recover lease and delegation state, and it
# exits when recovery is done; normally there is none. One present at every
# sample means recovery is looping; in the outage it respawned every second.
# It runs every minute so a looping one is seen.
ps -e -o stat=,comm= | awk '
{ n[substr($1, 1, 1)]++ }
$2 ~ /^[0-9a-f.:]+-(m|ma|man|mana|manag|manage|manager)?$/ && $2 ~ /[.:]/ { mgr++ }
END {
    # Always print the states that matter, even at 0, so a graph shows 0
    # rather than a gap; other states only when present.
    split("R S D Z", s, " ")
    for (i in s) n[s[i]] += 0
    for (k in n) printf "gadget_processes{state=\"%s\"} %d\n", k, n[k]
    printf "gadget_nfs_state_manager_threads %d\n", mgr
}'
