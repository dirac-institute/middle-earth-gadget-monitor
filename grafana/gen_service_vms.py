#!/usr/bin/env python3
"""Generates grafana/dashboards/service-vms.json (the source of truth for it).

    python3 grafana/gen_service_vms.py > grafana/dashboards/service-vms.json

VMs polled by the collector (config/service_vms.toml). service_vm_* comes from
each VM's monitor (monitoring/); gadget_service_vm_* from the collector's own
polls. Nothing here names a mount point or disk, and VMs are named only for
their colors, so adding a VM to the config needs no dashboard change (until it
gets a color here, Grafana picks one per line).

Capacity panels show used (solid) against the total (dashed, same color), in
bytes: the resource runs out where the two lines meet.
"""
import json, sys

DS = {"type": "prometheus", "uid": "victoriametrics"}
GOOD, WARN, CRIT = "#0ca30c", "#fab219", "#d03b3b"
M = 'service_vm=~"$service_vm"'
F = 'service_vm=~"$service_vm", mountpoint=~"$mountpoint"'
R = "[$__rate_interval]"
LEGEND = {"displayMode": "list", "placement": "bottom", "showLegend": True}
# dataviz categorical slots 1-4 (dark). mpg and dpg keep these on Postgres too.
VM_COLORS = {"gadget": "#3987e5", "mpg": "#d95926", "dpg": "#199e70", "vga": "#c98500"}
pid = 0

def target(expr, legend, ref="A", instant=False):
    return {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref,
            "range": not instant, "instant": instant}

def targets(*pairs):
    return [target(e, l, chr(ord("A") + i)) for i, (e, l) in enumerate(pairs)]

def capacity_overrides(total):
    """A fixed color per VM, and a dashed line for each series named '<vm> <total>'."""
    o = [{"matcher": {"id": "byRegexp", "options": f"/^{vm} /"},
          "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]}
         for vm, c in VM_COLORS.items()]
    o.append({"matcher": {"id": "byRegexp", "options": f"/ {total}$/"},
              "properties": [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 6]}}]})
    return o

def panel(kind, title, grid, targets, desc, unit=None, custom=None, options=None, overrides=None,
          extra=None, **defaults):
    global pid
    pid += 1
    fc = {"defaults": {**({"unit": unit} if unit else {}), **defaults}, "overrides": overrides or []}
    if custom is not None:
        fc["defaults"]["custom"] = custom
    # Polls run every minute; without this the datasource's 5m minimum step applies.
    p = {"id": pid, "type": kind, "title": title, "description": desc, "datasource": DS,
         "gridPos": dict(zip("xywh", grid)), "targets": targets, "fieldConfig": fc,
         "options": options or {}, "interval": "1m"}
    p.update(extra or {})
    return p

def row(title, y):
    global pid
    pid += 1
    return {"id": pid, "type": "row", "title": title, "collapsed": False, "panels": [],
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}}

def ts(title, grid, targets, desc, unit, custom=None, overrides=None, extra=None, **defaults):
    return panel("timeseries", title, grid, targets, desc, unit,
                 {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 0, "showPoints": "never",
                  # Polls are a minute apart: bridge one late sample, show any real gap.
                  "spanNulls": 90000, "axisSoftMin": 0, **(custom or {})},
                 {"legend": LEGEND, "tooltip": {"mode": "multi", "sort": "desc"}},
                 overrides, extra, color={"mode": "palette-classic"}, **defaults)

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
CPU_BUSY = (f'sum by (service_vm) (rate(service_vm_cpu_seconds_total{{{M}, mode!="idle"}}{R})) / '
            f'max by (service_vm) (service_vm_cpu_count{{{M}}})')
MEM_TOTAL = f'max by (service_vm) (service_vm_memory_total_bytes{{{M}}})'
SWAP_TOTAL = f'max by (service_vm) (service_vm_swap_total_bytes{{{M}}} > 0)'
FS_SIZE = f'max by (service_vm, mountpoint) (service_vm_filesystem_size_bytes{{{F}}})'
FS_USED = (f'1 - max by (service_vm, mountpoint) (service_vm_filesystem_avail_bytes{{{M}}}) / '
           f'max by (service_vm, mountpoint) (service_vm_filesystem_size_bytes{{{M}}})')

panels = [
    row("Status", 0),
    panel("state-timeline", "Monitor reachable", (0, 1, 12, 7),
        [target(f'max by (service_vm) (gadget_service_vm_up{{{M}}})', "{{service_vm}}")],
        "Whether the VM's service VM monitor answered the collector's poll (every minute, from "
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
        [target(f'max by (service_vm) ({FS_USED})', "{{service_vm}}", instant=True)],
        "The highest share of space used among each VM's local filesystems. The 'Filesystem "
        "used' panels below have each filesystem in bytes.", "percentunit", USED_STEPS, decimals=1),
    stat("Up since", (18, 1, 6, 7),
        [target(f'time() - max by (service_vm) (service_vm_boot_time_seconds{{{M}}})', "{{service_vm}}",
                instant=True)],
        "Time since each VM last booted.", "dtdurations",
        [{"color": "text", "value": None}]),

    row("CPU and memory", 8),
    ts("CPU busy", (0, 9, 12, 8), [target(CPU_BUSY, "{{service_vm}}")],
        "Share of the VM's CPU time spent on anything but idle, averaged over its cores. "
        "Includes waiting on disk (iowait) and time taken by the hypervisor (steal), which "
        "'CPU waiting' shows separately.", "percentunit", max=1),
    ts("CPU waiting", (12, 9, 12, 8),
        targets(
            (f'sum by (service_vm) (rate(service_vm_cpu_seconds_total{{{M}, mode="iowait"}}{R})) / '
             f'max by (service_vm) (service_vm_cpu_count{{{M}}})', "{{service_vm}} iowait"),
            (f'sum by (service_vm) (rate(service_vm_cpu_seconds_total{{{M}, mode="steal"}}{R})) / '
             f'max by (service_vm) (service_vm_cpu_count{{{M}}})', "{{service_vm}} steal")),
        "iowait: cores idle while a process waits on disk. steal: time the hypervisor gave "
        "this VM's cores to something else; steady steal means the host is oversubscribed.",
        "percentunit"),
    ts("Load per core", (0, 17, 12, 8),
        [target(f'max by (service_vm) (service_vm_load5{{{M}}}) / max by (service_vm) (service_vm_cpu_count{{{M}}})',
                "{{service_vm}}")],
        "5-minute load average divided by the number of cores. Above 1, processes are queuing "
        "for CPU (or stuck waiting on disk or NFS, which also counts as load on Linux).",
        "short", decimals=2),
    ts("Memory used", (12, 17, 6, 8),
        targets(
            (f'{MEM_TOTAL} - max by (service_vm) (service_vm_memory_available_bytes{{{M}}})',
             "{{service_vm}} used"),
            (MEM_TOTAL, "{{service_vm}} total")),
        "Memory in use (solid: total minus available, what can't be reclaimed without "
        "swapping) against installed memory (dashed). The gap is what is still available.",
        "bytes", overrides=capacity_overrides("total")),
    ts("Swap used", (18, 17, 6, 8),
        targets(
            (f'{SWAP_TOTAL} - max by (service_vm) (service_vm_swap_free_bytes{{{M}}})',
             "{{service_vm}} used"),
            (SWAP_TOTAL, "{{service_vm}} total")),
        "Swap in use (solid) against the swap configured (dashed). A VM without swap has no "
        "lines here.",
        "bytes", overrides=capacity_overrides("total")),

    row("Disks", 25),
    # One panel per mount point: sizes differ a hundredfold between them, and on
    # a shared axis the small filesystems would be flat lines along the bottom.
    ts("Filesystem used: $mountpoint", (0, 26, 8, 8),
        targets(
            (f'{FS_SIZE} - max by (service_vm, mountpoint) (service_vm_filesystem_avail_bytes{{{F}}})',
             "{{service_vm}} used"),
            (FS_SIZE, "{{service_vm}} size")),
        "Space used (solid) against the filesystem's size (dashed) on each VM with this mount "
        "point. Used is size minus what ordinary users can still write, so blocks reserved for "
        "root count as used, and the gap is exactly the space left. Local filesystems only "
        "(xfs, ext4, btrfs); network filesystems such as /service/shire are not included.",
        "bytes", overrides=capacity_overrides("size"),
        extra={"repeat": "mountpoint", "repeatDirection": "h", "maxPerRow": 3}),
    ts("Disk throughput", (0, 34, 12, 8),
        targets(
            (f'sum by (service_vm, device) (rate(service_vm_disk_read_bytes_total{{{M}}}{R}))',
             "{{service_vm}} {{device}} read"),
            (f'sum by (service_vm, device) (rate(service_vm_disk_written_bytes_total{{{M}}}{R}))',
             "{{service_vm}} {{device}} write")),
        "Bytes read from and written to each of the VM's disks per second.", "Bps"),
    ts("Disk busy", (12, 34, 12, 8),
        [target(f'sum by (service_vm, device) (rate(service_vm_disk_io_seconds_total{{{M}}}{R}))',
                "{{service_vm}} {{device}}")],
        "Share of time each disk had at least one request in flight. Near 100% the disk is the "
        "bottleneck (on virtual disks, which serve requests in parallel, it can be busy without "
        "being saturated).", "percentunit", max=1),
]

dash = {
    "uid": "gadget-service-vms",
    "title": "Service VMs",
    "description": "CPU, memory and disks of the VMs in config/service_vms.toml, polled from the monitoring VM every minute.",
    "tags": ["gadget-monitor"],
    "schemaVersion": 41,
    "editable": False,
    "graphTooltip": 1,
    "refresh": "1m",
    "time": {"from": "now-24h", "to": "now"},
    "timepicker": {},
    "timezone": "browser",
    "templating": {"list": [
        {"name": "service_vm", "label": "Service VM", "type": "query", "datasource": DS,
         "query": {"query": "label_values(gadget_service_vm_up, service_vm)", "refId": "service_vm"},
         "definition": "label_values(gadget_service_vm_up, service_vm)",
         "refresh": 2, "multi": True, "includeAll": True, "allValue": ".*",
         "current": {"selected": True, "text": ["All"], "value": ["$__all"]}, "sort": 1},
        # Drives the per-mount-point filesystem panels.
        {"name": "mountpoint", "label": "Mount point", "type": "query", "datasource": DS,
         "query": {"query": 'label_values(service_vm_filesystem_size_bytes{service_vm=~"$service_vm"}, mountpoint)',
                   "refId": "mountpoint"},
         "definition": 'label_values(service_vm_filesystem_size_bytes{service_vm=~"$service_vm"}, mountpoint)',
         "refresh": 2, "multi": True, "includeAll": True, "allValue": ".*",
         "current": {"selected": True, "text": ["All"], "value": ["$__all"]}, "sort": 1}]},
    "panels": panels,
}
json.dump(dash, sys.stdout, indent=2)
print()
