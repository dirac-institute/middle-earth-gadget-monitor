#!/bin/bash
# interval: 300
# timeout: 30
# Per-user CPU, memory and process count, from systemd's per-user cgroups.
#
# Every login, including each JupyterHub server, runs inside
# /sys/fs/cgroup/user.slice/user-<uid>.slice. Its cpu.stat counts CPU used by
# every process that ever ran there, including ones that have exited, so short
# jobs between samples are not missed. The cgroup is deleted when the user's
# last process ends, and recreated from zero at the next login (a counter
# reset, which rate() and increase() handle).
#
# The user label is the one deliberate exception to "no unbounded labels": it
# is bounded by the host's accounts, not by time. root, jhub-admin,
# astro-gadget and system accounts (uid < 1000) are never reported.
#
# memory is anonymous memory plus swap: what the user's processes hold, not
# file cache they happened to read through. processes leaves out the systemd
# user manager (init.scope), which lingers after logout.
cd /sys/fs/cgroup/user.slice || exit 1
uids=()
for d in user-*.slice; do
    u=${d#user-}; u=${u%.slice}
    [[ $u =~ ^[0-9]+$ ]] && (( u >= 1000 )) && uids+=("$u")
done
(( ${#uids[@]} )) || exit 0

declare -A name
while IFS=: read -r n _ u _; do name[$u]=$n; done < <(getent passwd "${uids[@]}")

for u in "${uids[@]}"; do
    n=${name[$u]:-$u}
    case $n in root|jhub-admin|astro-gadget) continue ;; esac
    d=user-$u.slice
    # The cgroup can disappear between listing and reading (user logged out).
    usec=$(awk '$1 == "usage_usec" { print $2 }' "$d/cpu.stat" 2>/dev/null)
    anon=$(awk '$1 == "anon" { print $2 }' "$d/memory.stat" 2>/dev/null)
    swap=$(cat "$d/memory.swap.current" 2>/dev/null)
    pids=$(cat "$d/pids.current" 2>/dev/null)
    [[ -n $usec && -n $anon && -n $pids ]] || continue
    mgr=$(wc -l < "$d/user@$u.service/init.scope/cgroup.procs" 2>/dev/null || echo 0)
    l="{user=\"$n\"}"
    printf 'gadget_user_cpu_seconds_total%s %d.%06d\n' "$l" $((usec / 1000000)) $((usec % 1000000))
    printf 'gadget_user_memory_bytes%s %d\n' "$l" $((anon + ${swap:-0}))
    printf 'gadget_user_processes%s %d\n' "$l" $((pids > mgr ? pids - mgr : 0))
done
