#!/bin/bash
# interval: 300
# timeout: 30
# NFS client per-operation counters for helios, shire and shiren, from the
# kernel's cumulative-since-mount stats in /proc/self/mountstats. Reading it
# never touches the network, so this is safe even when a server is hung.
#
# Mounts that share a superblock (every automounted /astro/users/* on helios)
# report identical numbers, so each superblock is counted once, then summed
# per fs. Ops are folded into a fixed set plus OTHER to keep series bounded.
# Times are the kernel's cumulative milliseconds, reported in seconds:
#   queue    waiting in the client before the request was sent
#   rtt      sent until the reply arrived
#   execute  whole call, queue + rtt + reply processing
# Average per call over an interval = increase(*_seconds_total) / increase(ops).
awk '
FNR == 1 { file++ }
# /proc/self/mountinfo: field 3 is the superblock major:minor, field 5 the mount point.
file == 1 { dev[$5] = $3; next }
/^device / {
    fs = ""
    split($2, src, ":")
    if (src[1] ~ /^helios(\.|$)/) fs = "helios"
    else if (src[1] ~ /^shire(\.|$)/) fs = (src[2] ~ /^\/nvme(\/|$)/ ? "shiren" : "shire")
    if (fs != "" && ($5 in dev) && !(dev[$5] in seen)) {
        seen[dev[$5]] = 1
        volumes[fs]++
    } else fs = ""
    next
}
# Per-op line: OP: ops ntrans timeouts bytes_sent bytes_recv queue_ms rtt_ms execute_ms [errors]
fs != "" && $1 ~ /^[A-Z_]+:$/ && NF >= 9 {
    op = substr($1, 1, length($1) - 1)
    if (op == "OPEN_NOATTR") op = "OPEN"
    else if (!(op in named)) op = "OTHER"
    k = fs SUBSEP op
    ops[k] += $2; queue[k] += $7; rtt[k] += $8; exec_[k] += $9
}
BEGIN {
    split("READ WRITE GETATTR LOOKUP ACCESS OPEN CLOSE", n, " ")
    for (i in n) named[n[i]] = 1
    nops = split("READ WRITE GETATTR LOOKUP ACCESS OPEN CLOSE OTHER", order, " ")
}
END {
    nfs = split("helios shire shiren", fss, " ")
    for (i = 1; i <= nfs; i++) {
        f = fss[i]
        printf "gadget_nfs_volumes{fs=\"%s\"} %d\n", f, volumes[f]
        if (!volumes[f]) continue
        for (j = 1; j <= nops; j++) {
            k = f SUBSEP order[j]
            l = sprintf("{fs=\"%s\",op=\"%s\"}", f, order[j])
            printf "gadget_nfs_ops_total%s %.0f\n", l, ops[k]
            printf "gadget_nfs_queue_seconds_total%s %.3f\n", l, queue[k] / 1000
            printf "gadget_nfs_rtt_seconds_total%s %.3f\n", l, rtt[k] / 1000
            printf "gadget_nfs_execute_seconds_total%s %.3f\n", l, exec_[k] / 1000
        }
    }
}' /proc/self/mountinfo /proc/self/mountstats
