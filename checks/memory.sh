#!/bin/bash
# interval: 300
# timeout: 30
# Memory and swap from one read of /proc/meminfo. SwapTotal is included so
# "no swap" (0 of 0) is distinguishable from "swap full" (0 of N).
awk '
$1 == "MemTotal:"     { m["MemTotal"] = $2 }
$1 == "MemFree:"      { m["MemFree"] = $2 }
$1 == "MemAvailable:" { m["MemAvailable"] = $2 }
$1 == "SwapTotal:"    { m["SwapTotal"] = $2 }
$1 == "SwapFree:"     { m["SwapFree"] = $2 }
END {
    for (k in m) printf "node_memory_%s_bytes %.0f\n", k, m[k] * 1024
}' /proc/meminfo
