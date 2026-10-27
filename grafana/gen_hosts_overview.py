#!/usr/bin/env python3
"""Generates grafana/dashboards/hosts-overview.json (the source of truth for it).

    python3 grafana/gen_hosts_overview.py > grafana/dashboards/hosts-overview.json

Host colors are fixed per host (categorical slots 1-2, dark theme) so a host
keeps its color in every panel; dashed lines are the secondary cue.
"""
import json, sys

DS = {"type": "prometheus", "uid": "victoriametrics"}
HOST_COLORS = {"arnor": "#3987e5", "gondor": "#d95926"}  # dataviz categorical slots 1-2 (dark)
GOOD, WARN, CRIT = "#0ca30c", "#fab219", "#d03b3b"
pid = 0

def target(expr, legend, ref="A", **kw):
    return {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref, "range": True, **kw}

def host_overrides(dash_suffix=None):
    o = [{"matcher": {"id": "byRegexp", "options": f"/^{h}/"},
          "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]}
         for h, c in HOST_COLORS.items()]
    if dash_suffix:
        o.append({"matcher": {"id": "byRegexp", "options": f"/{dash_suffix}$/"},
                  "properties": [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 6]}}]})
    return o

def panel(kind, title, grid, targets, desc="", unit=None, overrides=None, custom=None, options=None, extra=None, **defaults):
    global pid
    pid += 1
    fc = {"defaults": {**({"unit": unit} if unit else {}), **defaults}, "overrides": overrides or []}
    if custom is not None:
        fc["defaults"]["custom"] = custom
    p = {"id": pid, "type": kind, "title": title, "description": desc, "datasource": DS,
         "gridPos": dict(zip("xywh", grid)), "targets": targets, "fieldConfig": fc,
         "options": options or {}}
    p.update(extra or {})
    return p

def ts(title, grid, targets, desc, unit, overrides, min_=None, max_=None):
    custom = {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 0, "showPoints": "never",
              "spanNulls": 600000, "axisSoftMin": 0}
    d = {}
    if min_ is not None: d["min"] = min_
    if max_ is not None: d["max"] = max_
    return panel("timeseries", title, grid, targets, desc, unit, overrides, custom,
                 {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                  "tooltip": {"mode": "multi", "sort": "desc"}}, **d)

def row(title, y, collapsed=False, panels=None):
    global pid
    pid += 1
    return {"id": pid, "type": "row", "title": title, "collapsed": collapsed,
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": panels or []}

def state_mapping(on, off):
    return [{"type": "value", "options": {
        "1": {"text": on, "color": GOOD, "index": 0},
        "0": {"text": off, "color": CRIT, "index": 1}}}]

def state_timeline(title, grid, targets, desc, on, off):
    return panel("state-timeline", title, grid, targets, desc, None, None,
                 {"lineWidth": 0, "fillOpacity": 80},
                 {"showValue": "never", "mergeValues": True, "rowHeight": 0.8,
                  "legend": {"showLegend": False}, "tooltip": {"mode": "single"}},
                 mappings=state_mapping(on, off),
                 color={"mode": "thresholds"},
                 thresholds={"mode": "absolute", "steps": [{"color": CRIT, "value": None}, {"color": GOOD, "value": 1}]})

def stat(title, grid, targets, desc, unit, steps, mappings=None, legend_text=True, decimals=None):
    d = {"color": {"mode": "thresholds"}, "thresholds": {"mode": "absolute", "steps": steps}}
    if mappings: d["mappings"] = mappings
    if decimals is not None: d["decimals"] = decimals
    return panel("stat", title, grid, targets, desc, unit, None, None,
                 {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                  "colorMode": "value", "graphMode": "none", "textMode": "value_and_name" if legend_text else "value",
                  "justifyMode": "auto", "orientation": "horizontal"}, **d)

H = 'host=~"$host"'
panels = []
y = 0
panels.append(row("Health", y)); y += 1
panels.append(state_timeline("Check status", (0, y, 12, 8),
    [target(f'max by (host, check) (gadget_check_success{{{H}}})', "{{host}} / {{check}}")],
    "Did each check run and exit successfully? FAIL covers errors, timeouts, skipped (still stuck from last run), and host unreachable. See 'Collector internals' for the reason.",
    "OK", "FAIL"))
panels.append(state_timeline("SSH session", (12, y, 6, 8),
    [target(f'max by (host) (gadget_ssh_connected{{{H}}})', "{{host}}")],
    "Whether the collector's persistent SSH session (and remote agent) is up.", "Connected", "Down"))
panels.append(stat("Last backup", (18, y, 6, 4),
    [target('time() - max(gadget_backup_last_success_timestamp_seconds{kind="latest"})', "hourly", instant=True),
     target('time() - max(gadget_backup_last_success_timestamp_seconds{kind="daily"})', "daily", "B", instant=True)],
    "Age of the newest complete backup on /service/shire. Hourly should stay under ~1h; daily under ~1d.",
    "s", [{"color": GOOD, "value": None}, {"color": WARN, "value": 2 * 3600}, {"color": CRIT, "value": 26 * 3600}]))
panels.append(stat("Storage", (18, y + 4, 6, 4),
    [target('max(gadget_storage_bytes{location="local_vm"})', "local DB", instant=True),
     target('max(gadget_storage_bytes{location="shire_backup"})', "shire backups", "B", instant=True)],
    "Local database (limit 4.5 GiB; backups refuse above it) and total backup size on shire (hard budget 10 GB).",
    "bytes", [{"color": GOOD, "value": None}, {"color": WARN, "value": 4e9}, {"color": CRIT, "value": 4.8e9}], decimals=1))
y += 8

panels.append(row("CPU and load", y)); y += 1
panels.append(ts("CPU utilization", (0, y, 8, 9),
    [target(f'100 * (1 - sum by (host) (rate(gadget_cpu_seconds_total{{{H}, mode=~"idle|iowait"}}[$__interval])) '
            f'/ sum by (host) (rate(gadget_cpu_seconds_total{{{H}}}[$__interval])))', "{{host}}")],
    "Average busy % across all cores (everything except idle and iowait), averaged over each sample interval.",
    "percent", host_overrides(), 0, 100))
panels.append(ts("Load average", (8, y, 8, 9),
    [target(f'node_load1{{{H}}}', "{{host}} 1m"), target(f'node_load5{{{H}}}', "{{host}} 5m", "B")],
    "1-minute (solid) and 5-minute (dashed) load averages. Compare with core counts: arnor 256, gondor 128.",
    "short", host_overrides("5m")))
panels.append(ts("Load per core", (16, y, 8, 9),
    [target(f'node_load1{{{H}}} / on (host) gadget_cpu_count{{{H}}}', "{{host}} 1m"),
     target(f'node_load5{{{H}}} / on (host) gadget_cpu_count{{{H}}}', "{{host}} 5m", "B")],
    "Load average divided by online CPUs; around 1.0 means every core has work queued.",
    "short", host_overrides("5m")))
y += 9

panels.append(row("Memory and swap", y)); y += 1
panels.append(ts("Memory available", (0, y, 12, 9),
    [target(f'node_memory_MemAvailable_bytes{{{H}}}', "{{host}} available"),
     target(f'node_memory_MemTotal_bytes{{{H}}}', "{{host}} total", "B")],
    "Available (solid) is how much memory new work can still use, counting cache the kernel would give back. Total (dashed) is installed RAM.",
    "bytes", host_overrides("total")))
panels.append(ts("Swap free", (12, y, 8, 9),
    [target(f'node_memory_SwapFree_bytes{{{H}}}', "{{host}} free"),
     target(f'node_memory_SwapTotal_bytes{{{H}}}', "{{host}} total", "B")],
    "Free swap (solid) against total swap (dashed). gondor has no swap configured, so both are 0.",
    "bytes", host_overrides("total")))
panels.append(stat("Swap free now", (20, y, 4, 9),
    [target(f'100 * node_memory_SwapFree_bytes{{{H}}} / (node_memory_SwapTotal_bytes{{{H}}} > 0) '
            f'or (node_memory_SwapTotal_bytes{{{H}}} == 0) - 1', "{{host}}", instant=True)],
    "Percent of swap still free. 'no swap' means the host has none configured.",
    "percent", [{"color": CRIT, "value": None}, {"color": WARN, "value": 10}, {"color": GOOD, "value": 25}],
    mappings=[{"type": "value", "options": {"-1": {"text": "no swap", "color": "text", "index": 0}}}], decimals=0))
y += 9

internals = [
    ts("Check duration", (0, y + 1, 12, 8),
       [target(f'gadget_check_duration_seconds{{{H}}}', "{{host}} / {{check}}")],
       "Wall time of each check on the target.", "s", host_overrides()),
    ts("Check outcome by reason", (12, y + 1, 12, 8),
       [target(f'sum by (host, status) (gadget_check_status{{{H}, status!="ok"}})', "{{host}} {{status}}")],
       "Number of checks per host in each non-OK state: error, timeout, skipped_running (previous run still stuck), unreachable (no session), lost (session died mid-check).",
       "short", host_overrides()),
    ts("Clock skew", (0, y + 9, 8, 8),
       [target(f'gadget_clock_skew_seconds{{{H}}}', "{{host}}")],
       "Target clock minus collector clock, from ping round trips. Check timestamps use the target's clock.",
       "s", host_overrides()),
    ts("SSH reconnects", (8, y + 9, 8, 8),
       [target(f'increase(gadget_ssh_reconnects_total{{{H}}}[1h])', "{{host}}")],
       "Session drops per hour. Should be 0 in normal operation.", "short", host_overrides()),
    ts("Lines dropped", (16, y + 9, 8, 8),
       [target(f'gadget_check_lines_dropped{{{H}}} > 0', "{{host}} / {{check}}")],
       "Malformed or excess (>500) output lines discarded by the collector. Empty is good.",
       "short", host_overrides()),
]
panels.append(row("Collector internals", y, collapsed=True, panels=internals))

dash = {
    "uid": "gadget-hosts-overview",
    "title": "Hosts overview",
    "description": "arnor and gondor, sampled over SSH every 5 minutes by gadget-monitor.",
    "tags": ["gadget-monitor"],
    "schemaVersion": 41,
    "editable": False,
    "graphTooltip": 1,
    "refresh": "5m",
    "time": {"from": "now-24h", "to": "now"},
    "timepicker": {},
    "timezone": "browser",
    "templating": {"list": [{
        "name": "host", "label": "Host", "type": "query", "datasource": DS,
        "query": {"query": "label_values(gadget_ssh_connected, host)", "refId": "host"},
        "definition": "label_values(gadget_ssh_connected, host)",
        "refresh": 2, "multi": True, "includeAll": True, "allValue": ".*",
        "current": {"selected": True, "text": ["All"], "value": ["$__all"]}, "sort": 1}]},
    "panels": panels,
}
json.dump(dash, sys.stdout, indent=2)
print()
