#!/usr/bin/env python3
"""Generates grafana/dashboards/machines.json (the source of truth for it).

    python3 grafana/gen_machines.py > grafana/dashboards/machines.json

VMs polled by the collector (config/machines.toml). machine_* comes from each
VM's machine monitor (monitoring/); gadget_machine_* from the collector's own
polls. Nothing here names a VM, mount point or disk, so adding a VM to the
config needs no dashboard change.
"""
import json, sys

DS = {"type": "prometheus", "uid": "victoriametrics"}
GOOD, WARN, CRIT = "#0ca30c", "#fab219", "#d03b3b"
M = 'machine=~"$machine"'
R = "[$__rate_interval]"
LEGEND = {"displayMode": "list", "placement": "bottom", "showLegend": True}
pid = 0

def target(expr, legend, ref="A", instant=False):
    return {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref,
            "range": not instant, "instant": instant}

def targets(*pairs):
    return [target(e, l, chr(ord("A") + i)) for i, (e, l) in enumerate(pairs)]

def panel(kind, title, grid, targets, desc, unit=None, custom=None, options=None, **defaults):
    global pid
    pid += 1
    fc = {"defaults": {**({"unit": unit} if unit else {}), **defaults}, "overrides": []}
    if custom is not None:
        fc["defaults"]["custom"] = custom
    # Polls run every minute; without this the datasource's 5m minimum step applies.
    return {"id": pid, "type": kind, "title": title, "description": desc, "datasource": DS,
            "gridPos": dict(zip("xywh", grid)), "targets": targets, "fieldConfig": fc,
            "options": options or {}, "interval": "1m"}

def row(title, y):
    global pid
    pid += 1
    return {"id": pid, "type": "row", "title": title, "collapsed": False, "panels": [],
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}}

def ts(title, grid, targets, desc, unit, custom=None, **defaults):
    return panel("timeseries", title, grid, targets, desc, unit,
                 {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 0, "showPoints": "never",
                  # Polls are a minute apart: bridge one late sample, show any real gap.
                  "spanNulls": 90000, "axisSoftMin": 0, **(custom or {})},
                 {"legend": LEGEND, "tooltip": {"mode": "multi", "sort": "desc"}},
                 color={"mode": "palette-classic"}, **defaults)

def stat(title, grid, targets, desc, unit, steps, decimals=None):
    d = {"color": {"mode": "thresholds"}, "thresholds": {"mode": "absolute", "steps": steps},
         "noValue": "No data"}
    if decimals is not None: d["decimals"] = decimals
    return panel("stat", title, grid, targets, desc, unit, None,
                 {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                  "colorMode": "value", "graphMode": "none", "textMode": "value_and_name",
                  "justifyMode": "auto", "orientation": "horizontal"}, **d)

# Share of a resource in use: green, amber past 80%, red past 90%.
USED_STEPS = [{"color": GOOD, "value": None}, {"color": WARN, "value": 0.8}, {"color": CRIT, "value": 0.9}]
CPU_BUSY = (f'sum by (machine) (rate(machine_cpu_seconds_total{{{M}, mode!="idle"}}{R})) / '
            f'max by (machine) (machine_cpu_count{{{M}}})')
MEM_USED = (f'1 - max by (machine) (machine_memory_available_bytes{{{M}}}) / '
            f'max by (machine) (machine_memory_total_bytes{{{M}}})')
FS_USED = (f'1 - max by (machine, mountpoint) (machine_filesystem_avail_bytes{{{M}}}) / '
           f'max by (machine, mountpoint) (machine_filesystem_size_bytes{{{M}}})')

panels = [
    row("Status", 0),
    panel("state-timeline", "Monitor reachable", (0, 1, 12, 7),
        [target(f'max by (machine) (gadget_machine_up{{{M}}})', "{{machine}}")],
        "Whether the VM's machine monitor answered the collector's poll (every minute, from "
        "the monitoring VM). Down means the VM, its network, or the monitor container is down; "
        "the panels below have a gap for that time.",
        None, {"lineWidth": 0, "fillOpacity": 80},
        {"showValue": "never", "mergeValues": True, "rowHeight": 0.8,
         "legend": {"showLegend": False}, "tooltip": {"mode": "single"}},
        mappings=[{"type": "value", "options": {
            "1": {"text": "Up", "color": GOOD, "index": 0},
            "0": {"text": "Down", "color": CRIT, "index": 1}}}],
        color={"mode": "thresholds"},
        thresholds={"mode": "absolute", "steps": [{"color": CRIT, "value": None}, {"color": GOOD, "value": 1}]}),
    stat("Fullest filesystem", (12, 1, 6, 7),
        [target(f'max by (machine) ({FS_USED})', "{{machine}}", instant=True)],
        "The highest share of space used among each VM's local filesystems. 'Filesystem used' "
        "below has every filesystem.", "percentunit", USED_STEPS, decimals=1),
    stat("Up since", (18, 1, 6, 7),
        [target(f'time() - max by (machine) (machine_boot_time_seconds{{{M}}})', "{{machine}}",
                instant=True)],
        "Time since each VM last booted.", "dtdurations",
        [{"color": "text", "value": None}]),

    row("CPU and memory", 8),
    ts("CPU busy", (0, 9, 12, 8), [target(CPU_BUSY, "{{machine}}")],
        "Share of the VM's CPU time spent on anything but idle, averaged over its cores. "
        "Includes waiting on disk (iowait) and time taken by the hypervisor (steal), which "
        "'CPU waiting' shows separately.", "percentunit", max=1),
    ts("CPU waiting", (12, 9, 12, 8),
        targets(
            (f'sum by (machine) (rate(machine_cpu_seconds_total{{{M}, mode="iowait"}}{R})) / '
             f'max by (machine) (machine_cpu_count{{{M}}})', "{{machine}} iowait"),
            (f'sum by (machine) (rate(machine_cpu_seconds_total{{{M}, mode="steal"}}{R})) / '
             f'max by (machine) (machine_cpu_count{{{M}}})', "{{machine}} steal")),
        "iowait: cores idle while a process waits on disk. steal: time the hypervisor gave "
        "this VM's cores to something else; steady steal means the host is oversubscribed.",
        "percentunit"),
    ts("Load per core", (0, 17, 12, 8),
        [target(f'max by (machine) (machine_load5{{{M}}}) / max by (machine) (machine_cpu_count{{{M}}})',
                "{{machine}}")],
        "5-minute load average divided by the number of cores. Above 1, processes are queuing "
        "for CPU (or stuck waiting on disk or NFS, which also counts as load on Linux).",
        "short", decimals=2),
    ts("Memory used", (12, 17, 12, 8),
        targets(
            (MEM_USED, "{{machine}} memory"),
            (f'1 - max by (machine) (machine_swap_free_bytes{{{M}}}) / '
             f'max by (machine) (machine_swap_total_bytes{{{M}}} > 0)', "{{machine}} swap")),
        "Memory in use (total minus available: what can't be reclaimed without swapping), and "
        "swap in use, as a share of each. A VM without swap has no swap line.",
        "percentunit", max=1),

    row("Disks", 25),
    ts("Filesystem used", (0, 26, 12, 8), [target(FS_USED, "{{machine}} {{mountpoint}}")],
        "Share of space used on each local filesystem (xfs, ext4, btrfs). Network filesystems "
        "such as /service/shire are not included.", "percentunit", max=1),
    ts("Filesystem free", (12, 26, 12, 8),
        [target(f'max by (machine, mountpoint) (machine_filesystem_avail_bytes{{{M}}})',
                "{{machine}} {{mountpoint}}")],
        "Space left on each local filesystem for ordinary users.", "bytes"),
    ts("Disk throughput", (0, 34, 12, 8),
        targets(
            (f'sum by (machine, device) (rate(machine_disk_read_bytes_total{{{M}}}{R}))',
             "{{machine}} {{device}} read"),
            (f'sum by (machine, device) (rate(machine_disk_written_bytes_total{{{M}}}{R}))',
             "{{machine}} {{device}} write")),
        "Bytes read from and written to each of the VM's disks per second.", "Bps"),
    ts("Disk busy", (12, 34, 12, 8),
        [target(f'sum by (machine, device) (rate(machine_disk_io_seconds_total{{{M}}}{R}))',
                "{{machine}} {{device}}")],
        "Share of time each disk had at least one request in flight. Near 100% the disk is the "
        "bottleneck (on virtual disks, which serve requests in parallel, it can be busy without "
        "being saturated).", "percentunit", max=1),
]

dash = {
    "uid": "gadget-machines",
    "title": "VM resources",
    "description": "CPU, memory and disks of the VMs in config/machines.toml, polled from the monitoring VM every minute.",
    "tags": ["gadget-monitor"],
    "schemaVersion": 41,
    "editable": False,
    "graphTooltip": 1,
    "refresh": "1m",
    "time": {"from": "now-24h", "to": "now"},
    "timepicker": {},
    "timezone": "browser",
    "templating": {"list": [
        {"name": "machine", "label": "VM", "type": "query", "datasource": DS,
         "query": {"query": "label_values(gadget_machine_up, machine)", "refId": "machine"},
         "definition": "label_values(gadget_machine_up, machine)",
         "refresh": 2, "multi": True, "includeAll": True, "allValue": ".*",
         "current": {"selected": True, "text": ["All"], "value": ["$__all"]}, "sort": 1}]},
    "panels": panels,
}
json.dump(dash, sys.stdout, indent=2)
print()
