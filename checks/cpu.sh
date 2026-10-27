#!/bin/bash
# interval: 300
# timeout: 30
# CPU time summed over all cores (first line of /proc/stat), as counters in
# seconds. CPU % is computed in Grafana with rate(), so it is the true average
# over each interval rather than a short sample.
hz="$(getconf CLK_TCK)"
awk -v hz="$hz" '$1 == "cpu" {
    n = split("user nice system idle iowait irq softirq steal", mode, " ")
    for (i = 1; i <= n; i++)
        printf "gadget_cpu_seconds_total{mode=\"%s\"} %.2f\n", mode[i], $(i + 1) / hz
    exit
}' /proc/stat
echo "gadget_cpu_count $(getconf _NPROCESSORS_ONLN)"
