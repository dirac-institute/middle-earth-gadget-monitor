#!/bin/bash
# interval: 60
# timeout: 30
# hosts: shire
# NFS server traffic and connections per client, as far as an unprivileged
# user can see them (per-client NFS state, /proc/fs/nfsd/clients, is
# root-only):
#   /proc/fs/nfsd/export_stats  bytes read and written and stale file handles
#                               per export and client. Each entry restarts
#                               from zero when the export cache refreshes it
#                               (a counter reset), so this runs every minute
#                               to lose little at each restart.
#   rdma res show cm_id         nfsd's RDMA connections (one per connection;
#                               a client mounting with nconnect has several)
#   /proc/net/tcp{,6}           established TCP connections to port 2049
# client is the short host name (rohan1.infiniband and rohan1 are both
# rohan1), or the address when it has none; clients come from the exports,
# so the set is bounded. fs is shire for exports under /data, shiren under
# /nvme; the NFSv4 pseudo-root (/) carries no data and is skipped.
set -o pipefail
tmp=/dev/shm/gadget-nfsd-clients.$$
trap 'rm -f "$tmp"' EXIT

{
    awk -F'\t' '
    /^\// {
        path = $1; client = $2; start = $3
        fs = (path ~ /^\/data(\/|$)/) ? "shire" : (path ~ /^\/nvme(\/|$)/) ? "shiren" : ""
        next
    }
    fs != "" && /^\t[a-z_]+: [0-9]+$/ {
        split($2, kv, ": ")
        k = path SUBSEP client
        # Duplicate entries for one export and client: keep the newest.
        if (!(k in st) || start >= st[k]) { st[k] = start; v[k, kv[1]] = kv[2]; f[k] = fs }
    }
    END {
        for (k in st) {
            split(k, p, SUBSEP)
            printf "export\t%s\t%s\t%s\t%s\t%s\t%s\n", f[k], p[1], p[2], v[k, "io_read"], v[k, "io_write"], v[k, "fh_stale"]
        }
    }' /proc/fs/nfsd/export_stats || exit 1

    rdma res show cm_id 2>/dev/null | awk '
    / state CONNECT / && /comm \[rpcrdma\]/ {
        for (i = 1; i < NF; i++) if ($i == "dst-addr") { a = $(i + 1); sub(/:[0-9]+$/, "", a); print "rdma\t" a }
    }'

    awk '
    function hex(s,   i, n) { n = 0; for (i = 1; i <= length(s); i++) n = n * 16 + index("0123456789ABCDEF", substr(s, i, 1)) - 1; return n }
    # Addresses are little-endian 32-bit words; IPv4 in tcp6 is ::ffff:a.b.c.d.
    function v4(h) { return hex(substr(h, 7, 2)) "." hex(substr(h, 5, 2)) "." hex(substr(h, 3, 2)) "." hex(substr(h, 1, 2)) }
    FNR > 1 && $4 == "01" {
        split($2, l, ":"); split($3, r, ":")
        if (l[2] != "0801") next
        h = r[1]
        if (length(h) == 8) print "tcp\t" v4(h)
        else if (substr(h, 1, 24) == "0000000000000000FFFF0000") print "tcp\t" v4(substr(h, 25, 8))
        else print "tcp\tipv6"
    }' /proc/net/tcp /proc/net/tcp6
} > "$tmp"

# Short names for every address seen, in one getent call.
mapfile -t ips < <(awk -F'\t' '
    ($1 == "rdma" || $1 == "tcp") { print $2 }
    $1 == "export" { print $4 }' "$tmp" | grep -E '^[0-9.]+$' | sort -u)
{
    (( ${#ips[@]} )) && getent hosts "${ips[@]}" | awk '{ print "name\t" $1 "\t" $2 }'
    cat "$tmp"
} | awk -F'\t' '
function short(c) { if (c in name) c = name[c]; if (c !~ /^[0-9.]+$/) sub(/\..*/, "", c); return c }
$1 == "name" { name[$2] = $3; next }
$1 == "export" {
    l = sprintf("{fs=\"%s\",export=\"%s\",client=\"%s\"}", $2, $3, short($4))
    printf "gadget_nfsd_client_read_bytes_total%s %s\n", l, $5 + 0
    printf "gadget_nfsd_client_write_bytes_total%s %s\n", l, $6 + 0
    printf "gadget_nfsd_client_stale_filehandles_total%s %s\n", l, $7 + 0
    next
}
{ conns[short($2) SUBSEP $1]++ }
END {
    for (k in conns) {
        split(k, p, SUBSEP)
        printf "gadget_nfsd_client_connections{client=\"%s\",transport=\"%s\"} %d\n", p[1], p[2], conns[k]
    }
}'
