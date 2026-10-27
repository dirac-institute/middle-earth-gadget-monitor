#!/usr/bin/env python3
"""Generates grafana/dashboards/hosts-overview.json (the source of truth for it).

    python3 grafana/gen_hosts_overview.py > grafana/dashboards/hosts-overview.json

Host colors are fixed per host (categorical slots 1-2, dark theme) so a host
keeps its color in every panel; dashed lines are the secondary cue.
"""
import json, sys
import user_usage

DS = {"type": "prometheus", "uid": "victoriametrics"}
HOST_COLORS = {"arnor": "#3987e5", "gondor": "#d95926"}  # dataviz categorical slots 1-2 (dark)
# Login stages use the next slots (3-5) so they never borrow a host's color.
STAGE_COLORS = {"connect": "#199e70", "auth": "#c98500", "shell": "#d55181"}
# NFS ops take categorical slots 1-7 in order; OTHER is neutral gray, not a hue.
OP_COLORS = {"READ": "#3987e5", "WRITE": "#d95926", "GETATTR": "#199e70", "LOOKUP": "#c98500",
             "ACCESS": "#d55181", "OPEN": "#008300", "CLOSE": "#9085e9", "OTHER": "#8f8e86"}
NFS_FS = ("helios", "shire", "shiren")
# GPUs (gondor only) take slots 3 and 7: never a host's color, and far apart.
GPU_COLORS = {"gpu0": "#199e70", "gpu1": "#9085e9"}
# User counts: active is drawn over logged in (active users are a subset).
USER_COLORS = {"Logged in": "#9085e9", "Active": "#199e70"}
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

panels.append(row("SSH login", y)); y += 1
panels.append(ts("Login time", (0, y, 24, 8),
    [target(f'gadget_ssh_login_seconds{{{H}, stage="total"}}', "{{host}}")],
    "Time for a brand-new interactive SSH login to reach a usable shell, probed every 5 minutes "
    "(every 15 after repeated timeouts). Timeouts (60 s) show as FAIL on ssh_login in Check status.",
    "s", host_overrides()))
stages = panel("timeseries", "Login stages: $host", (0, y + 8, 24, 9),
    [target(f'gadget_ssh_login_seconds{{host="$host", stage="{st}"}}', st, ref)
     for st, ref in (("connect", "A"), ("auth", "B"), ("shell", "C"))],
    "Where the login time goes, stacked. connect: DNS and TCP. auth: key exchange and authentication, "
    "which on these hosts reads ~/.ssh/authorized_keys from the NFS home directory. shell: session setup, "
    "login shell and profile until a typed command runs. A hung login shows only the stages it finished.",
    "s",
    [{"matcher": {"id": "byName", "options": st},
      "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]}
     for st, c in STAGE_COLORS.items()],
    {"drawStyle": "line", "lineWidth": 1, "fillOpacity": 70, "showPoints": "never",
     "spanNulls": 600000, "axisSoftMin": 0, "stacking": {"mode": "normal", "group": "A"}},
    {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
     "tooltip": {"mode": "multi", "sort": "none"}},
    extra={"repeat": "host", "repeatDirection": "h", "maxPerRow": 2})
panels.append(stages)
y += 17

def per_call(metric, sel, by):
    return (f'sum by ({by}) (rate(gadget_nfs_{metric}_seconds_total{{{sel}}}[$__interval])) '
            f'/ sum by ({by}) (rate(gadget_nfs_ops_total{{{sel}}}[$__interval]))')

panels.append(row("NFS", y)); y += 1
for i, fs in enumerate(NFS_FS):
    panels.append(ts(f"Queue wait per call: {fs}", (8 * i, y, 8, 8),
        [target(per_call("queue", f'{H}, fs="{fs}"', "host"), "{{host}}")],
        f"Average time an NFS call to {fs} waited in the client before being sent, over all "
        "operations, from the kernel's per-mount counters. Rising queue wait means the client "
        "has more requests than it can put on the wire (or the server is slow to free slots).",
        "s", host_overrides()))
    panels.append(ts(f"Round-trip time per call: {fs}", (8 * i, y + 8, 8, 8),
        [target(per_call("rtt", f'{H}, fs="{fs}"', "host"), "{{host}}")],
        f"Average time from sending an NFS call to {fs} until its reply arrived (network plus "
        "server), over all operations. Calls still in progress are not counted until they "
        "finish, so a hung server shows as a gap, not a spike.",
        "s", host_overrides()))
y += 16

panels.append(row("NFS queue wait by operation", y)); y += 1
for i, fs in enumerate(NFS_FS):
    sel = f'host="$host", fs="{fs}"'
    panels.append(panel("timeseries", f"Queue wait by operation: $host, {fs}", (0, y + 9 * i, 24, 9),
        [target(per_call("queue", sel, "op"), "{{op}}")],
        f"Average client-side queue wait per call to {fs}, per NFS operation. OPEN includes "
        "OPEN_NOATTR; OTHER is every remaining operation (DELEGRETURN, SETATTR, RENAME, READDIR, "
        "LOCK, COMMIT, ...). A line has gaps while that operation had no calls.",
        "s",
        [{"matcher": {"id": "byName", "options": op},
          "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]}
         for op, c in OP_COLORS.items()],
        {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 0, "showPoints": "never",
         "spanNulls": 600000, "axisSoftMin": 0},
        {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
         "tooltip": {"mode": "multi", "sort": "desc"}},
        extra={"repeat": "host", "repeatDirection": "h", "maxPerRow": 2}))
y += 27

def gpu_overrides(dash_suffix=None):
    o = [{"matcher": {"id": "byRegexp", "options": f"/^{g}/"},
          "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]}
         for g, c in GPU_COLORS.items()]
    if dash_suffix:
        o.append({"matcher": {"id": "byRegexp", "options": f"/{dash_suffix}$/"},
                  "properties": [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 6]}}]})
    return o

panels.append(row("GPUs (gondor)", y)); y += 1
panels.append(ts("GPU memory free", (0, y, 9, 9),
    [target(f'gadget_gpu_memory_free_bytes{{{H}}}', "gpu{{gpu}} free"),
     target(f'gadget_gpu_memory_total_bytes{{{H}}}', "gpu{{gpu}} total", "B")],
    "Free GPU memory (solid) against each GPU's total (dashed), sampled every minute. "
    "The driver keeps about 0.6 GB per GPU for itself, so free never quite reaches total.",
    "bytes", gpu_overrides("total")))
panels.append(ts("GPU utilization", (9, y, 9, 9),
    [target(f'gadget_gpu_utilization_percent{{{H}}}', "gpu{{gpu}} average"),
     target(f'gadget_gpu_utilization_max_percent{{{H}}}', "gpu{{gpu}} peak", "B")],
    "Percent of time a GPU kernel was running, from the driver's samples over the ~14 s before "
    "each once-a-minute check: average (solid) and peak (dashed). Covers about a quarter of the "
    "time, so short bursts between checks can be missed.",
    "percent", gpu_overrides("peak"), 0, 100))
panels.append(ts("GPU processes", (18, y, 6, 9),
    [target(f'gadget_gpu_processes{{{H}}}', "gpu{{gpu}}")],
    "Number of processes holding each GPU (they may hold memory without computing).",
    "short", gpu_overrides()))
y += 9

panels.append(row("Users", y)); y += 1
for i, (period, what) in enumerate((("1d", "day"), ("7d", "week"))):
    panels.append(panel("timeseries", f"Users per {what}", (12 * i, y, 12, 9),
        [target(user_usage.distinct_users(user_usage.logged_in(H), period), "Logged in", interval="1h"),
         target(user_usage.distinct_users(user_usage.active(H), period), "Active", "B", interval="1h")],
        f"Distinct users on the selected hosts in each {what} (Pacific time"
        f"{', weeks start Monday' if period == '7d' else ''}; the last bar is the {what} so far). "
        "Logged in: had at least one process running. Active: in at least one 5-minute interval, "
        f"used {user_usage.CPU_CORES}+ CPU cores on average, or held 1+ GiB of RAM, or kept a GPU "
        f"{user_usage.GPU_PERCENT}%+ busy, or held 5%+ of a GPU's memory. root, jhub-admin, "
        "astro-gadget and system accounts are not counted.",
        "short",
        [{"matcher": {"id": "byName", "options": n},
          "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]}
         for n, c in USER_COLORS.items()],
        {"drawStyle": "bars", "barAlignment": 1, "lineWidth": 1, "fillOpacity": 80,
         "showPoints": "never", "axisSoftMin": 0},
        {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
         "tooltip": {"mode": "multi", "sort": "none"}},
        extra={"timeFrom": "90d", "maxDataPoints": 3000}, decimals=0))
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
