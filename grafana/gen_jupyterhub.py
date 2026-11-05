#!/usr/bin/env python3
"""Generates grafana/dashboards/jupyterhub.json (the source of truth for it).

    python3 grafana/gen_jupyterhub.py > grafana/dashboards/jupyterhub.json

JupyterHubs probed by the collector (config/jupyterhubs.toml), next to the
jupyterhub check's process counts from the host of the same name. Nothing
here names a hub, so adding one to the config needs no dashboard change. The
helpers and colors follow gen_websites.py.
"""
import json, sys

DS = {"type": "prometheus", "uid": "victoriametrics"}
GOOD, WARN, CRIT = "#0ca30c", "#fab219", "#d03b3b"
# Stages use categorical slots 3-7, as on Websites.
STAGE_COLORS = {"dns": "#199e70", "connect": "#c98500", "tls": "#d55181", "response": "#008300",
                "body": "#9085e9"}
S = 'hub=~"$hub"'
H = 'host=~"$hub"'   # the jupyterhub check, from the hub's host
LEGEND = {"displayMode": "list", "placement": "bottom", "showLegend": True}
UP_MAPPINGS = [{"type": "value", "options": {
    "1": {"text": "Up", "color": GOOD, "index": 0},
    "0": {"text": "Down", "color": CRIT, "index": 1}}}]
RED_GREEN = {"mode": "absolute", "steps": [{"color": CRIT, "value": None}, {"color": GOOD, "value": 1}]}
TIMELINE = {"showValue": "never", "mergeValues": True, "rowHeight": 0.8,
            "legend": {"showLegend": False}, "tooltip": {"mode": "single"}}
pid = 0

def target(expr, legend, ref="A", instant=False):
    # Not both: a stat would then list every hub twice.
    return {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref,
            "range": not instant, "instant": instant}

def panel(kind, title, grid, targets, desc, unit=None, overrides=None, custom=None, options=None, extra=None, **defaults):
    global pid
    pid += 1
    fc = {"defaults": {**({"unit": unit} if unit else {}), **defaults}, "overrides": overrides or []}
    if custom is not None:
        fc["defaults"]["custom"] = custom
    # The probe runs every minute; without this the datasource's 5m minimum step applies.
    p = {"id": pid, "type": kind, "title": title, "description": desc, "datasource": DS,
         "gridPos": dict(zip("xywh", grid)), "targets": targets, "fieldConfig": fc,
         "options": options or {}, "interval": "1m"}
    p.update(extra or {})
    return p

def ts(title, grid, targets, desc, unit, custom=None, extra=None, **defaults):
    return panel("timeseries", title, grid, targets, desc, unit, None,
                 {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 0, "showPoints": "never",
                  "spanNulls": 180000, "axisSoftMin": 0, **(custom or {})},
                 {"legend": LEGEND, "tooltip": {"mode": "multi", "sort": "desc"}}, extra,
                 color={"mode": "palette-classic"}, **defaults)

def stat(title, grid, targets, desc, unit, steps, decimals=None):
    d = {"color": {"mode": "thresholds"}, "thresholds": {"mode": "absolute", "steps": steps}}
    if decimals is not None: d["decimals"] = decimals
    return panel("stat", title, grid, targets, desc, unit, None, None,
                 {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                  "colorMode": "value", "graphMode": "none", "textMode": "value_and_name",
                  "justifyMode": "auto", "orientation": "horizontal"}, **d)

# The jupyterhub check runs every 5 minutes.
CHECK = {"interval": "5m"}

panels = [
    panel("state-timeline", "Hub answers", (0, 0, 16, 8),
        [target(f'max by (hub) (gadget_jupyterhub_up{{{S}}})', "{{hub}}")],
        "Whether each hub's API root (<url>hub/api) answered 200 with the hub's version within "
        "15 s, probed from the monitoring VM every minute. That needs the host's web server, the "
        "proxy and the hub itself all working. It does not test starting a server or a kernel. "
        "'Failures by reason' says why a probe failed, and 'Hub processes' which piece is missing.",
        None, None, {"lineWidth": 0, "fillOpacity": 80}, TIMELINE,
        mappings=UP_MAPPINGS, color={"mode": "thresholds"}, thresholds=RED_GREEN),
    stat("Uptime, selected range", (16, 0, 8, 4),
        [target(f'avg_over_time(gadget_jupyterhub_up{{{S}}}[$__range])', "{{hub}}", instant=True)],
        "Share of probes over the selected time range that found the hub answering.",
        "percentunit", [{"color": CRIT, "value": None}, {"color": WARN, "value": 0.99},
                        {"color": GOOD, "value": 0.999}], decimals=2),
    stat("Certificate expires in", (16, 4, 8, 4),
        [target(f'max by (hub) (gadget_jupyterhub_cert_expiry_timestamp_seconds{{{S}}}) - time()',
                "{{hub}}", instant=True)],
        "Time until the TLS certificate the hub's host served on its last successful handshake "
        "expires.",
        "dtdurations", [{"color": CRIT, "value": None}, {"color": WARN, "value": 7 * 86400},
                        {"color": GOOD, "value": 21 * 86400}]),
    ts("Response time", (0, 8, 12, 9),
        [target(f'gadget_jupyterhub_seconds{{{S}, stage="total"}}', "{{hub}}")],
        "Time for the hub to answer its API root, from DNS lookup to the last byte. The answer is "
        "tiny, so this is the web server's, proxy's and hub's own latency: a busy or stuck hub "
        "shows here first. A failed probe shows the time until it failed; timeouts show as 15 s.",
        "s"),
    ts("Failures by reason", (12, 8, 12, 9),
        [target(f'sum by (hub, status) (gadget_jupyterhub_status{{{S}, status!="ok"}}) > 0',
                "{{hub}} {{status}}")],
        "Failed probes per minute, by reason. http_error: answered with anything but 200 (a 502 or "
        "503 usually means the proxy or hub behind the web server is down); timeout: no complete "
        "answer within 15 s; dns_error, connect_error, tls_error: failed at that step; error: a "
        "200 that wasn't the hub's answer, or a broken response. Empty is good.", "short",
        {"drawStyle": "points", "pointSize": 6}, decimals=0, noValue="No failures"),
    ts("Running servers", (0, 17, 12, 8),
        [target(f'max by (host) (gadget_jupyterhub_processes{{{H}, process="singleuser"}})', "{{host}}")],
        "Single-user servers running on each hub's host: one per user with a server started, busy "
        "or idle. Counted from the host's processes every 5 minutes.",
        "short", {"spanNulls": 600000}, CHECK, decimals=0),
    panel("state-timeline", "Hub processes", (12, 17, 12, 8),
        [target(f'max by (host, process) (gadget_jupyterhub_processes{{{H}, process=~"hub|proxy"}})',
                "{{host}} {{process}}")],
        "Whether the hub and its proxy (configurable-http-proxy) are running on each host, checked "
        "every 5 minutes. Both running while the hub doesn't answer means the hub is stuck. Without "
        "the proxy, users can't reach their running servers either.",
        None, None, {"lineWidth": 0, "fillOpacity": 80}, TIMELINE, CHECK,
        mappings=[{"type": "value", "options": {"0": {"text": "Not running", "color": CRIT, "index": 0}}},
                  {"type": "range", "options": {"from": 1, "to": None,
                                                "result": {"text": "Running", "color": GOOD, "index": 1}}}],
        color={"mode": "thresholds"}, thresholds=RED_GREEN),
    panel("table", "JupyterHub version", (0, 25, 24, 4),
        [{**target(f'max by (hub, version) (last_over_time(gadget_jupyterhub_info{{{S}}}[1h]))',
                   "", instant=True), "format": "table"}],
        "Version each hub reported in the last hour.", None, None, None,
        {"showHeader": True, "cellHeight": "sm"},
        {"transformations": [{"id": "organize", "options": {
            "excludeByName": {"Time": True, "Value": True},
            "renameByName": {"hub": "Hub", "version": "Version"}}}]}),
    panel("timeseries", "Response time by stage: $hub", (0, 29, 24, 9),
        # =~ even for one hub: Grafana regex-escapes the value, which only matches as a regex.
        [target(f'gadget_jupyterhub_seconds{{hub=~"$hub", stage="{st}"}}', st, chr(ord("A") + i))
         for i, st in enumerate(STAGE_COLORS)],
        "Where the response time goes, stacked. dns: name lookup; connect: TCP; tls: handshake and "
        "certificate check; response: request sent until the headers arrive (the web server, proxy "
        "and hub); body: reading the answer.",
        "s",
        [{"matcher": {"id": "byName", "options": st},
          "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]}
         for st, c in STAGE_COLORS.items()],
        {"drawStyle": "line", "lineWidth": 1, "fillOpacity": 70, "showPoints": "never",
         "spanNulls": 180000, "axisSoftMin": 0, "stacking": {"mode": "normal", "group": "A"}},
        {"legend": LEGEND, "tooltip": {"mode": "multi", "sort": "none"}},
        {"repeat": "hub", "repeatDirection": "h", "maxPerRow": 2}),
]

dash = {
    "uid": "gadget-jupyterhub",
    "title": "JupyterHub",
    "description": "JupyterHubs in config/jupyterhubs.toml, probed from the monitoring VM every "
                   "minute, and the hub processes on their hosts.",
    "tags": ["gadget-monitor"],
    "schemaVersion": 41,
    "editable": False,
    "graphTooltip": 1,
    "refresh": "1m",
    "time": {"from": "now-24h", "to": "now"},
    "timepicker": {},
    "timezone": "browser",
    "templating": {"list": [{
        "name": "hub", "label": "Hub", "type": "query", "datasource": DS,
        "query": {"query": "label_values(gadget_jupyterhub_up, hub)", "refId": "hub"},
        "definition": "label_values(gadget_jupyterhub_up, hub)",
        "refresh": 2, "multi": True, "includeAll": True, "allValue": ".*",
        "current": {"selected": True, "text": ["All"], "value": ["$__all"]}, "sort": 1}]},
    "panels": panels,
}
json.dump(dash, sys.stdout, indent=2)
print()
