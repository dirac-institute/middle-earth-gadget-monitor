#!/bin/bash
# interval: 60
# timeout: 30
# hosts: shire
# NFS server counters, from /proc/net/rpc/nfsd and /proc/fs/nfsd/pool_stats
# (world-readable, cumulative since nfsd started), and the nfsd threads by
# state from ps. Every minute, because delegation storms are bursty.
#
# The kernel keeps these for the whole server, not per export: they can't
# be split into shire and shiren (nfsd_delegations and nfs_disks can).
#
# NFSv4 operations are counted by number (proc4ops is indexed by opcode,
# RFC 8881). A fixed set is named, the file handle bookkeeping that every
# compound carries (PUTFH, GETFH, SAVEFH, RESTOREFH, PUTROOTFH) is left out,
# and everything else is OTHER. SEQUENCE opens every NFSv4.1+ compound, so
# its rate is the request rate. TEST_STATEID and FREE_STATEID are how a
# client finds and clears state the server revoked: many tests with few
# frees is a client looping on revoked state.
#
# The "net" line counts every request; only UDP and TCP are broken out, so
# the rest is RDMA. "io" is bytes read and written for clients.
awk '
FNR == 1 { file++ }
file == 1 && $1 == "proc4ops" {
    for (i = 3; i <= NF; i++) {
        code = i - 3
        if (code in skip) continue
        op = (code in name) ? name[code] : "OTHER"
        ops[op] += $i
    }
}
file == 1 && $1 == "rpc" { calls = $2; bad = $3 }
file == 1 && $1 == "net" { net = $2; udp = $3; tcp = $4; tcpconn = $5 }
file == 1 && $1 == "io"  { rd = $2; wr = $3 }
file == 1 && $1 == "th"  { threads = $2 }
file == 2 && $1 ~ /^[0-9]+$/ { arrived += $2; woken += $4; timedout += $5 }
BEGIN {
    n = split("3 ACCESS 4 CLOSE 5 COMMIT 8 DELEGRETURN 9 GETATTR 15 LOOKUP 18 OPEN 25 READ " \
              "26 READDIR 38 WRITE 45 FREE_STATEID 53 SEQUENCE 55 TEST_STATEID", a, " ")
    for (i = 1; i < n; i += 2) { name[a[i]] = a[i + 1]; order[++nops] = a[i + 1] }
    order[++nops] = "OTHER"
    split("10 22 23 24 31 32", b, " ")
    for (i in b) skip[b[i]] = 1
}
END {
    for (i = 1; i <= nops; i++)
        printf "gadget_nfsd_ops_total{op=\"%s\"} %.0f\n", order[i], ops[order[i]]
    printf "gadget_nfsd_rpc_calls_total %.0f\n", calls
    printf "gadget_nfsd_rpc_bad_calls_total %.0f\n", bad
    printf "gadget_nfsd_requests_total{transport=\"tcp\"} %.0f\n", tcp
    printf "gadget_nfsd_requests_total{transport=\"udp\"} %.0f\n", udp
    printf "gadget_nfsd_requests_total{transport=\"rdma\"} %.0f\n", net - tcp - udp
    printf "gadget_nfsd_tcp_connections_total %.0f\n", tcpconn
    printf "gadget_nfsd_read_bytes_total %.0f\n", rd
    printf "gadget_nfsd_write_bytes_total %.0f\n", wr
    printf "gadget_nfsd_threads_configured %d\n", threads
    printf "gadget_nfsd_packets_arrived_total %.0f\n", arrived
    printf "gadget_nfsd_threads_woken_total %.0f\n", woken
    printf "gadget_nfsd_threads_timedout_total %.0f\n", timedout
}' /proc/net/rpc/nfsd /proc/fs/nfsd/pool_stats || exit 1

# nfsd threads are kernel threads named "nfsd"; idle ones are in I, ones
# waiting on the disks (or a lock) in D.
ps -e -o stat=,comm= | awk '
$2 == "nfsd" { n[substr($1, 1, 1)]++ }
END {
    split("R S D I", s, " ")
    for (i in s) n[s[i]] += 0
    for (k in n) printf "gadget_nfsd_threads{state=\"%s\"} %d\n", k, n[k]
}'
