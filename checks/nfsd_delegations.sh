#!/bin/bash
# interval: 60
# timeout: 30
# hosts: shire
# NFSv4 delegations the server has handed out, per filesystem, from
# /proc/locks (world-readable). nfsd implements a delegation as a lease
# flagged DELEG, listed with the device and inode of the file:
#   N: DELEG  ACTIVE    READ  <pid> <major>:<minor>:<inode> 0 EOF
# ACTIVE is held; BREAKING is being recalled (the server asked the client to
# give it back and is waiting, up to lease-break-time). Which client holds a
# delegation is only in /proc/fs/nfsd/clients, which is root-only.
#
# fs is shire (/data) or shiren (/nvme), matched by device number; anything
# else nfsd exports is "other". shared counts files with more than one
# delegation (read delegations held by several clients): a write by anyone
# recalls all of them at once.
dev() {
    local d
    d=$(mountpoint -d "$1" 2>/dev/null) || return
    printf '%02x:%02x' "${d%%:*}" "${d##*:}"
}
awk -v shire="$(dev /data)" -v shiren="$(dev /nvme)" '
$2 == "DELEG" {
    split($6, d, ":")
    k = d[1] ":" d[2]
    fs = (k == shire) ? "shire" : (k == shiren) ? "shiren" : "other"
    if ($3 == "BREAKING") breaking[fs]++
    else if ($3 == "ACTIVE") held[fs SUBSEP tolower($4)]++
    per_file[fs SUBSEP $6]++
}
END {
    for (k in per_file) if (per_file[k] > 1) { split(k, p, SUBSEP); shared[p[1]]++ }
    nfs = split("shire shiren other", fss, " ")
    for (i = 1; i <= nfs; i++) {
        f = fss[i]
        if (f == "other" && !breaking[f] && !held[f SUBSEP "read"] && !held[f SUBSEP "write"]) continue
        printf "gadget_nfsd_delegations{fs=\"%s\",type=\"read\"} %d\n", f, held[f SUBSEP "read"]
        printf "gadget_nfsd_delegations{fs=\"%s\",type=\"write\"} %d\n", f, held[f SUBSEP "write"]
        printf "gadget_nfsd_delegations_breaking{fs=\"%s\"} %d\n", f, breaking[f]
        printf "gadget_nfsd_delegated_files_shared{fs=\"%s\"} %d\n", f, shared[f]
    }
}' /proc/locks
