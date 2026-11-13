#!/bin/bash
# interval: 60
# timeout: 30
# Kernel log messages of a few known kinds, counted as they are logged:
#   hung_task           "task ... blocked for more than N seconds"
#   lockup              soft or hard lockups, RCU stalls
#   kernel_bug          BUG, Oops, WARNING, general protection fault, panic
#   hardware_error      machine checks and EDAC memory errors
#   io_error            block device I/O errors
#   nfs_not_responding  "nfs: server ... not responding"
#   nfs_recall_any      the client receiving a RECALL_ANY callback; the
#                       kernel only logs these while NFS callback debugging
#                       is on (rpcdebug -m nfs -s callback), as it was in the
#                       November 2026 outage
#   rdma_error          mlx5 completion errors (QP error, error CQE)
# OOM kills are counted by vmstat (node_vmstat_oom_kill).
#
# The message timestamps can't be used to pick out "the last minute": they
# come from the kernel's sched clock, which drifts from /proc/uptime by
# seconds per week. Instead each message's sequence number in /dev/kmsg says
# whether it is new, and the last one seen and the running totals are kept
# in a small file on /dev/shm, so the counters are cumulative like any other
# _total. They start from 0 (not from what is already in the log) when that
# file is missing, as after a reboot or if logind removes it after the
# session ends; Grafana's rate() treats that as a counter reset.
#
# Reading /dev/kmsg needs kernel.dmesg_restrict = 0 (true on all three
# hosts); if it is restricted, the check fails rather than reporting zeros.
state="/dev/shm/gadget-kernel-log.$UID"
boot=$(< /proc/sys/kernel/random/boot_id)
# Each read of /dev/kmsg returns one record: "prio,seq,usec,flags;message",
# plus continuation lines (starting with a space) that are ignored. A
# non-blocking read ends with EAGAIN once it has caught up, so dd's exit
# status means nothing; an open that fails leaves the output empty.
log=$(dd if=/dev/kmsg iflag=nonblock bs=16384 2>/dev/null)
[ -n "$log" ] || { echo "cannot read /dev/kmsg" >&2; exit 1; }
awk -v boot="$boot" -v state="$state" '
BEGIN {
    nk = split("hung_task lockup kernel_bug hardware_error io_error " \
               "nfs_not_responding nfs_recall_any rdma_error", kinds, " ")
    last = -1
    # State file: line 1 is "<boot id> <last seq>", then "<kind> <total>".
    if ((getline line < state) > 0) {
        split(line, f, " ")
        if (f[1] == boot) {
            last = f[2] + 0
            while ((getline line < state) > 0) { split(line, f, " "); total[f[1]] = f[2] + 0 }
        }
        close(state)
    }
}
/^[0-9]+,[0-9]+,/ {
    split($0, h, ",")
    seq = h[2] + 0
    if (seq > max) max = seq
    # Only new messages, and only the kernel'\''s own (facility 0, so
    # prio < 8): systemd and others also write to /dev/kmsg.
    if (last < 0 || seq <= last || h[1] + 0 >= 8) next
    msg = substr($0, index($0, ";") + 1)
    if (msg ~ /blocked for more than [0-9]+ seconds/) k = "hung_task"
    else if (msg ~ /soft lockup|hard LOCKUP|rcu_.*(stall|detected stalls)/) k = "lockup"
    else if (msg ~ /^(BUG:|Oops|WARNING: CPU:|general protection fault|Kernel panic)/) k = "kernel_bug"
    else if (msg ~ /Hardware Error|EDAC MC[0-9]+: [0-9]+ [CU]E /) k = "hardware_error"
    else if (msg ~ /I\/O error/) k = "io_error"
    else if (msg ~ /^nfs: server .* not responding/) k = "nfs_not_responding"
    else if (msg ~ /RECALL_ANY/) k = "nfs_recall_any"
    else if (msg ~ /QP [0-9]+ error|handle_error_cqe/) k = "rdma_error"
    else next
    total[k]++
}
END {
    # On the first run of a boot (last = -1) this starts from the end of
    # the log, with every total at 0.
    if (max > last) last = max
    printf "%s %d\n", boot, last > state
    for (i = 1; i <= nk; i++) {
        printf "%s %d\n", kinds[i], total[kinds[i]] > state
        printf "gadget_kernel_log_messages_total{kind=\"%s\"} %d\n", kinds[i], total[kinds[i]]
    }
}' <<< "$log"
