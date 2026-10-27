#!/bin/bash
# interval: 60
# timeout: 30
# hosts: gondor
# NVIDIA GPU memory, utilization and process count, from one nvidia-smi call
# (each call is a full driver round trip, slow when persistence mode is off).
#
# Utilization is bursty, so a single reading means little. The driver keeps
# ~14 s of samples; the average and max over that window are reported, and
# the check runs every minute to cover roughly a quarter of the time. When
# the sample buffer is empty (the driver just woke up) the instantaneous
# reading is used for both. The gpu label is nvidia-smi's index (enumeration
# order). Processes are counted only: PIDs and users are unbounded labels.
nvidia-smi -q -d MEMORY,UTILIZATION,PIDS | awk -F' *: *' '
/^GPU [0-9A-Fa-f]+:/ { g = n++; procs[g] = 0; next }
n == 0 { next }
# Section headers are indented 4 spaces, their fields 8 or more.
/^    [^ ]/ { sec = $0; sub(/^ +/, "", sec); next }
{ key = $1; sub(/^ +/, "", key); val = $2; sub(/ .*/, "", val) }
sec == "FB Memory Usage" && key == "Total" { total[g] = val }
sec == "FB Memory Usage" && key == "Free"  { free[g] = val }
sec == "Utilization" && key == "GPU"       { now_[g] = val }
sec == "GPU Utilization Samples" && key == "Avg" { avg[g] = val }
sec == "GPU Utilization Samples" && key == "Max" { max_[g] = val }
sec == "Processes" && key == "Process ID"  { procs[g]++ }
function num(v) { return v ~ /^[0-9.]+$/ }
END {
    for (g = 0; g < n; g++) {
        l = sprintf("{gpu=\"%d\"}", g)
        if (num(total[g])) printf "gadget_gpu_memory_total_bytes%s %.0f\n", l, total[g] * 1048576
        if (num(free[g]))  printf "gadget_gpu_memory_free_bytes%s %.0f\n", l, free[g] * 1048576
        a = num(avg[g]) ? avg[g] : now_[g]
        m = num(max_[g]) ? max_[g] : now_[g]
        if (num(a)) printf "gadget_gpu_utilization_percent%s %s\n", l, a
        if (num(m)) printf "gadget_gpu_utilization_max_percent%s %s\n", l, m
        printf "gadget_gpu_processes%s %d\n", l, procs[g]
    }
    if (n == 0) exit 1
}'
