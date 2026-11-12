#!/usr/bin/env python3
"""Generates grafana/dashboards/collector-internals.json (the source of truth for it).

    python3 grafana/gen_collector_internals.py > grafana/dashboards/collector-internals.json

Is gadget-monitor itself working: did each check run, are the SSH sessions
up, are backups current. Moved off Hosts overview (issue #32), which is the
landing page and should show the hosts, not the monitor. Colors match Hosts
overview (fixed per host, dark theme).
"""
import json, sys

DS = {"type": "prometheus", "uid": "victoriametrics"}
HOST_COLORS = {"arnor": "#3987e5", "gondor": "#d95926"}  # as on Hosts overview
GOOD, WARN, CRIT = "#0ca30c", "#fab219", "#d03b3b"
pid = 0

def target(expr, legend, ref="A", instant=False):
    # Not both: a stat panel shows every frame, so range + instant doubles each value.
    return {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref,
            "range": not instant, "instant": instant}

def host_overrides():
    return [{"matcher": {"id": "byRegexp", "options": f"/^{h}/"},
             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]}
            for h, c in HOST_COLORS.items()]

def panel(kind, title, grid, targets, desc="", unit=None, overrides=None, custom=None, options=None, **defaults):
    global pid
    pid += 1
    fc = {"defaults": {**({"unit": unit} if unit else {}), **defaults}, "overrides": overrides or []}
    if custom is not None:
        fc["defaults"]["custom"] = custom
    return {"id": pid, "type": kind, "title": title, "description": desc, "datasource": DS,
            "gridPos": dict(zip("xywh", grid)), "targets": targets, "fieldConfig": fc,
            "options": options or {}}

def ts(title, grid, targets, desc, unit):
    return panel("timeseries", title, grid, targets, desc, unit, host_overrides(),
                 {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 0, "showPoints": "never",
                  "spanNulls": 600000, "axisSoftMin": 0},
                 {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                  "tooltip": {"mode": "multi", "sort": "desc"}})

def row(title, y):
    global pid
    pid += 1
    return {"id": pid, "type": "row", "title": title, "collapsed": False,
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []}

def state_timeline(title, grid, targets, desc, on, off):
    return panel("state-timeline", title, grid, targets, desc, None, None,
                 {"lineWidth": 0, "fillOpacity": 80},
                 {"showValue": "never", "mergeValues": True, "rowHeight": 0.8,
                  "legend": {"showLegend": False}, "tooltip": {"mode": "single"}},
                 mappings=[{"type": "value", "options": {
                     "1": {"text": on, "color": GOOD, "index": 0},
                     "0": {"text": off, "color": CRIT, "index": 1}}}],
                 color={"mode": "thresholds"},
                 thresholds={"mode": "absolute", "steps": [{"color": CRIT, "value": None}, {"color": GOOD, "value": 1}]})

def stat(title, grid, targets, desc, unit, steps, decimals=None):
    d = {"color": {"mode": "thresholds"}, "thresholds": {"mode": "absolute", "steps": steps}}
    if decimals is not None: d["decimals"] = decimals
    return panel("stat", title, grid, targets, desc, unit, None, None,
                 {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                  "colorMode": "value", "graphMode": "none", "textMode": "value_and_name",
                  "justifyMode": "auto", "orientation": "horizontal"}, **d)

H = 'host=~"$host"'
panels = [
    row("Health", 0),
    state_timeline("Check status", (0, 1, 16, 12),
        [target(f'max by (host, check) (gadget_check_success{{{H}}})', "{{host}} / {{check}}")],
        "Did each check run and exit successfully? FAIL covers errors, timeouts, skipped (still stuck from last run), and host unreachable. 'Check outcome by reason' below says which.",
        "OK", "FAIL"),
    state_timeline("SSH session", (16, 1, 8, 4),
        [target(f'max by (host) (gadget_ssh_connected{{{H}}})', "{{host}}")],
        "Whether the collector's persistent SSH session (and remote agent) is up.", "Connected", "Down"),
    stat("Last backup", (16, 5, 8, 4),
        [target('time() - max(gadget_backup_last_success_timestamp_seconds{kind="latest"})', "hourly", instant=True),
         target('time() - max(gadget_backup_last_success_timestamp_seconds{kind="daily"})', "daily", "B", instant=True)],
        "Age of the newest complete backup on /service/shire. Hourly should stay under ~1h; daily under ~1d.",
        "s", [{"color": GOOD, "value": None}, {"color": WARN, "value": 2 * 3600}, {"color": CRIT, "value": 26 * 3600}]),
    stat("Storage", (16, 9, 8, 4),
        [target('max(gadget_storage_bytes{location="local_vm"})', "local DB", instant=True),
         target('max(gadget_storage_bytes{location="shire_backup"})', "shire backups", "B", instant=True)],
        "Local database (limit 4.5 GiB; backups refuse above it) and total backup size on shire (hard budget 10 GB). 'Gadget metrics storage' has the trend.",
        "bytes", [{"color": GOOD, "value": None}, {"color": WARN, "value": 4e9}, {"color": CRIT, "value": 4.8e9}], decimals=1),
    row("Checks and sessions", 13),
    ts("Check duration", (0, 14, 12, 8),
       [target(f'gadget_check_duration_seconds{{{H}}}', "{{host}} / {{check}}")],
       "Wall time of each check on the target.", "s"),
    ts("Check outcome by reason", (12, 14, 12, 8),
       [target(f'sum by (host, status) (gadget_check_status{{{H}, status!="ok"}})', "{{host}} {{status}}")],
       "Number of checks per host in each non-OK state: error, timeout, skipped_running (previous run still stuck), unreachable (no session), lost (session died mid-check).",
       "short"),
    ts("Clock skew", (0, 22, 8, 8),
       [target(f'gadget_clock_skew_seconds{{{H}}}', "{{host}}")],
       "Target clock minus collector clock, from ping round trips. Check timestamps use the target's clock.",
       "s"),
    ts("SSH reconnects", (8, 22, 8, 8),
       [target(f'increase(gadget_ssh_reconnects_total{{{H}}}[1h])', "{{host}}")],
       "Session drops per hour. Should be 0 in normal operation.", "short"),
    ts("Lines dropped", (16, 22, 8, 8),
       [target(f'gadget_check_lines_dropped{{{H}}} > 0', "{{host}} / {{check}}")],
       "Malformed or excess (>500) output lines discarded by the collector. Empty is good.",
       "short"),
]

dash = {
    "uid": "gadget-collector-internals",
    "title": "Gadget collector internals",
    "description": "Whether gadget-monitor itself is working: check runs, SSH sessions to arnor and gondor, backups.",
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
