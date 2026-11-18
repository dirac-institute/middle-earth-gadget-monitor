#!/usr/bin/env python3
"""Generates grafana/dashboards/globus.json (the source of truth for it).

    python3 grafana/gen_globus.py > grafana/dashboards/globus.json

Globus Connect Server nodes probed by the collector (config/globus.toml).
Nothing here names a node: every panel covers whatever nodes report, so adding
one to the config needs no dashboard change. Nodes take Grafana's classic
palette for the same reason; the semantic colors are only for the states and
thresholds.
"""
import json, sys

DS = {"type": "prometheus", "uid": "victoriametrics"}
GOOD, WARN, CRIT, DEAD, UNKNOWN = "#0ca30c", "#fab219", "#d03b3b", "#8a1c1c", "#8e8e8e"
S = 'server=~"$server"'
LEGEND = {"displayMode": "list", "placement": "bottom", "showLegend": True}
pid = 0

def target(expr, legend, ref="A", instant=False):
    return {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref,
            "range": not instant, "instant": instant}

def panel(kind, title, grid, targets, desc, unit=None, overrides=None, custom=None, options=None, **defaults):
    global pid
    pid += 1
    fc = {"defaults": {**({"unit": unit} if unit else {}), **defaults}, "overrides": overrides or []}
    if custom is not None:
        fc["defaults"]["custom"] = custom
    # Probes run every minute; without this the datasource's 5m minimum step applies.
    return {"id": pid, "type": kind, "title": title, "description": desc, "datasource": DS,
            "gridPos": dict(zip("xywh", grid)), "targets": targets, "fieldConfig": fc,
            "options": options or {}, "interval": "1m"}

def ts(title, grid, targets, desc, unit, custom=None, **defaults):
    return panel("timeseries", title, grid, targets, desc, unit, None,
                 {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 0, "showPoints": "never",
                  "spanNulls": 180000, "axisSoftMin": 0, **(custom or {})},
                 {"legend": LEGEND, "tooltip": {"mode": "multi", "sort": "desc"}},
                 color={"mode": "palette-classic"}, **defaults)

def stat(title, grid, targets, desc, unit, steps=None, decimals=None, text_mode="value_and_name", **defaults):
    d = {"color": {"mode": "thresholds"},
         "thresholds": {"mode": "absolute", "steps": steps or [{"color": "text", "value": None}]}, **defaults}
    if decimals is not None: d["decimals"] = decimals
    return panel("stat", title, grid, targets, desc, unit, None, None,
                 {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                  "colorMode": "value", "graphMode": "none", "textMode": text_mode,
                  "justifyMode": "auto", "orientation": "horizontal"}, **d)

def state(name):
    return f'sum by (server) (gadget_globus_state{{{S}, state="{name}"}})'

panels = [
    panel("state-timeline", "Health", (0, 0, 16, 8),
        # One number per probe: exactly one state is 1, so this picks it out.
        [target(f'3 * {state("healthy")} + 2 * {state("unknown")} + {state("unhealthy")}', "{{server}}")],
        "Each node's GCS Manager, asked for /api/info from the monitoring VM every minute. "
        "Healthy: answered 200 with code \"success\". Unhealthy: the node's HTTPS port answered, "
        "but not that (a 502 with the Manager down, a certificate that doesn't verify, a broken "
        "answer). Unreachable: no answer on the port at all. Unknown: no Globus domain configured "
        "for the node, so only reachability is tested. Data transfers (GridFTP) aren't tested.",
        None, None, {"lineWidth": 0, "fillOpacity": 80},
        {"showValue": "never", "mergeValues": True, "rowHeight": 0.8,
         "legend": {"showLegend": False}, "tooltip": {"mode": "single"}},
        mappings=[{"type": "value", "options": {
            "3": {"text": "Healthy", "color": GOOD, "index": 0},
            "2": {"text": "Unknown (no domain)", "color": UNKNOWN, "index": 1},
            "1": {"text": "Unhealthy", "color": CRIT, "index": 2},
            "0": {"text": "Unreachable", "color": DEAD, "index": 3}}}],
        color={"mode": "thresholds"},
        # The same colors again as thresholds: the mappings alone didn't color the cells.
        thresholds={"mode": "absolute", "steps": [{"color": DEAD, "value": None}, {"color": CRIT, "value": 1},
                                                  {"color": UNKNOWN, "value": 2}, {"color": GOOD, "value": 3}]}),
    stat("Healthy, selected range", (16, 0, 8, 4),
        # Leaves out nodes that were unknown (no domain) all along: 0% would read as down.
        [target(f'avg_over_time(gadget_globus_up{{{S}}}[$__range]) and on (server) '
                f'(min_over_time(gadget_globus_state{{{S}, state="unknown"}}[$__range]) == 0)',
                "{{server}}", instant=True)],
        "Share of probes over the selected time range that found the node healthy. Nodes with no "
        "Globus domain configured aren't shown.",
        "percentunit", [{"color": CRIT, "value": None}, {"color": WARN, "value": 0.99},
                        {"color": GOOD, "value": 0.999}], decimals=2, noValue="No domain configured"),
    stat("Certificate expires in", (16, 4, 8, 4),
        [target(f'max by (server) (gadget_globus_cert_expiry_timestamp_seconds{{{S}}}) - time()',
                "{{server}}", instant=True)],
        "Time until the certificate the node served for its Globus domain on its last successful "
        "handshake expires. Globus renews these automatically.",
        "dtdurations", [{"color": CRIT, "value": None}, {"color": WARN, "value": 7 * 86400},
                        {"color": GOOD, "value": 21 * 86400}], noValue="No domain configured"),
    ts("Response time", (0, 8, 12, 8),
        [target(f'gadget_globus_seconds{{{S}, stage="total"}}', "{{server}}")],
        "Time for the whole probe: TCP connect, TLS handshake and the /api/info answer. A failed "
        "probe shows the time until it failed; timeouts show as 15 s.", "s"),
    ts("Failures by state", (12, 8, 12, 8),
        [target(f'sum by (server, state) (gadget_globus_state{{{S}, state=~"unhealthy|unreachable"}}) > 0',
                "{{server}} {{state}}")],
        "Failed probes per minute: unhealthy (the port answered, the Manager didn't say success) "
        "or unreachable (no answer). The collector's log has the reason for each. Empty is good.",
        "short", {"drawStyle": "points", "pointSize": 6}, decimals=0, noValue="No failures"),
    ts("HTTP status", (0, 16, 12, 6),
        [target(f'gadget_globus_http_status{{{S}}}', "{{server}}")],
        "Status code of the /api/info answer. 0 means no answer at all; 404 is a node with no "
        "domain configured (Apache's default site).", "none", decimals=0),
    stat("GCS version", (12, 16, 12, 6),
        [target(f'max by (server, version) (last_over_time(gadget_globus_manager_info{{{S}}}[10m]))',
                "{{server}}: {{version}}", instant=True)],
        "Globus Connect Server Manager version each node reported on its last healthy probe "
        "(within 10 minutes).", "none", text_mode="name", noValue="No healthy probe"),
]

dash = {
    "uid": "gadget-globus",
    "title": "Globus",
    "description": "Globus Connect Server nodes in config/globus.toml, probed from the monitoring VM every minute.",
    "tags": ["gadget-monitor"],
    "schemaVersion": 41,
    "editable": False,
    "graphTooltip": 1,
    "refresh": "1m",
    "time": {"from": "now-24h", "to": "now"},
    "timepicker": {},
    "timezone": "browser",
    "templating": {"list": [{
        "name": "server", "label": "Node", "type": "query", "datasource": DS,
        "query": {"query": "label_values(gadget_globus_up, server)", "refId": "server"},
        "definition": "label_values(gadget_globus_up, server)",
        "refresh": 2, "multi": True, "includeAll": True, "allValue": ".*",
        "current": {"selected": True, "text": ["All"], "value": ["$__all"]}, "sort": 1}]},
    "panels": panels,
}
json.dump(dash, sys.stdout, indent=2)
print()
