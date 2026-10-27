#!/bin/bash
# interval: 60
# timeout: 30
# hosts: gondor
# Per-user GPU memory and GPU busy %, summed over the user's processes and
# GPUs (see users.sh for the user label and who is excluded).
#
# Memory comes from --query-compute-apps. Busy % is nvidia-smi pmon's "sm"
# column for one sample: the share of time the process had a kernel running
# on that GPU (a user busy on both GPUs can reach 200). Processes the sampler
# saw no activity from show "-" and count as 0. Users with no GPU processes
# print nothing.
set -o pipefail
{
    nvidia-smi --query-compute-apps=pid,used_memory --format=csv,noheader,nounits | sed 's/^/mem /'
    nvidia-smi pmon -c 1 -s u | sed 's/^/sm /'
} > /dev/shm/gadget-users-gpu.$$ || { rm -f /dev/shm/gadget-users-gpu.$$; exit 1; }
trap 'rm -f /dev/shm/gadget-users-gpu.$$' EXIT

# pid -> uid -> name, from /proc and one getent call.
declare -A uid_of name
for p in $(awk '$1 == "mem" { sub(/,/, "", $2); print $2 }' /dev/shm/gadget-users-gpu.$$ | sort -u); do
    u=$(stat -c %u "/proc/$p" 2>/dev/null) && uid_of[$p]=$u
done
(( ${#uid_of[@]} )) || exit 0
while IFS=: read -r n _ u _; do name[$u]=$n; done < <(getent passwd $(printf '%s\n' "${uid_of[@]}" | sort -u))

for p in "${!uid_of[@]}"; do
    u=${uid_of[$p]}
    (( u >= 1000 )) || continue
    n=${name[$u]:-$u}
    case $n in root|jhub-admin|astro-gadget) continue ;; esac
    echo "$p $n"
done | awk '
FILENAME == "-" { user[$1] = $2; next }
$1 == "mem" { sub(/,/, "", $2); if ($2 in user) mem[user[$2]] += $3 }
$1 == "sm" && $2 !~ /^#/ && ($3 in user) { busy[user[$3]] += ($5 ~ /^[0-9]+$/) ? $5 : 0 }
END {
    for (n in mem) {
        printf "gadget_user_gpu_memory_bytes{user=\"%s\"} %.0f\n", n, mem[n] * 1048576
        printf "gadget_user_gpu_percent{user=\"%s\"} %d\n", n, busy[n]
    }
}' - /dev/shm/gadget-users-gpu.$$
