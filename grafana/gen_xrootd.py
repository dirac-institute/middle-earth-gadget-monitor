#!/usr/bin/env python3
"""Generates grafana/dashboards/xrootd.json (the source of truth for it).

    python3 grafana/gen_xrootd.py > grafana/dashboards/xrootd.json

xrootd servers probed by the collector (config/xrootd.toml), from outside:
is the host up, does xrootd answer as far as login, how long each stage
takes, and when the host certificate expires. Nothing here names a server, so
adding one to the config needs no dashboard change.
"""
import json, sys

DS = {"type": "prometheus", "uid": "victoriametrics"}
GOOD, WARN, CRIT = "#0ca30c", "#fab219", "#d03b3b"
S = 'server=~"$server"'
LEGEND = {"displayMode": "list", "placement": "bottom", "showLegend": True}
DAY = 86400
pid = 0

def target(expr, legend, ref="A", instant=False):
    return {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref,
            "range": not instant, "instant": instant}

def targets(*pairs):
    return [target(e, l, chr(ord("A") + i)) for i, (e, l) in enumerate(pairs)]

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

def row(title, y):
    global pid
    pid += 1
    return {"id": pid, "type": "row", "title": title, "collapsed": False, "panels": [],
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}}

def ts(title, grid, targets, desc, unit, custom=None, overrides=None, **defaults):
    return panel("timeseries", title, grid, targets, desc, unit, overrides,
                 {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 0, "showPoints": "never",
                  # Probes are a minute apart: bridge one late sample, show any real gap.
                  "spanNulls": 90000, "axisSoftMin": 0, **(custom or {})},
                 {"legend": LEGEND, "tooltip": {"mode": "multi", "sort": "desc"}},
                 color={"mode": "palette-classic"}, **defaults)

def stat(title, grid, targets, desc, unit, steps, decimals=None, mappings=None, no_value=None):
    d = {"color": {"mode": "thresholds"}, "thresholds": {"mode": "absolute", "steps": steps}}
    if decimals is not None: d["decimals"] = decimals
    if mappings: d["mappings"] = mappings
    if no_value: d["noValue"] = no_value
    return panel("stat", title, grid, targets, desc, unit, None, None,
                 {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                  "colorMode": "value", "graphMode": "none", "textMode": "value_and_name",
                  "justifyMode": "auto", "orientation": "horizontal"}, **d)

UP_DOWN = [{"type": "value", "options": {
    "1": {"text": "Up", "color": GOOD, "index": 0},
    "0": {"text": "Down", "color": CRIT, "index": 1}}}]

panels = [
    row("Status", 0),
    panel("state-timeline", "Up", (0, 1, 16, 9),
        targets(
            (f'max by (server) (gadget_xrootd_vm_up{{{S}}})', "{{server}} host"),
            (f'max by (server) (gadget_xrootd_up{{{S}}})', "{{server}} xrootd")),
        "Probed from the monitoring VM every minute. Host: the host answers on TCP (SSH port). "
        "xrootd: the server completes the xrootd handshake, TLS and a login request, up to "
        "asking for authentication. The probe has no credentials, so this doesn't show whether "
        "authenticated users can read or write data.",
        None, None, {"lineWidth": 0, "fillOpacity": 80},
        {"showValue": "never", "mergeValues": True, "rowHeight": 0.8,
         "legend": {"showLegend": False}, "tooltip": {"mode": "single"}},
        mappings=UP_DOWN, color={"mode": "thresholds"},
        thresholds={"mode": "absolute", "steps": [{"color": CRIT, "value": None}, {"color": GOOD, "value": 1}]}),
    stat("xrootd up, selected range", (16, 1, 8, 5),
        [target(f'avg_over_time(gadget_xrootd_up{{{S}}}[$__range])', "{{server}}", instant=True)],
        "Share of probes over the selected time range in which xrootd answered as far as login.",
        "percentunit", [{"color": CRIT, "value": None}, {"color": WARN, "value": 0.99},
                        {"color": GOOD, "value": 0.999}], decimals=2),
    stat("Host certificate expires in", (16, 6, 8, 4),
        [target(f'max by (server) (gadget_xrootd_cert_expiry_timestamp_seconds{{{S}}}) - time()',
                "{{server}}", instant=True)],
        "Time left on the certificate xrootd presents for TLS, its host certificate (also used "
        "for GSI). Amber under 30 days, red under 7. No value: the server didn't get as far as "
        "TLS in the last probe.",
        "dtdurations", [{"color": CRIT, "value": None}, {"color": WARN, "value": 7 * DAY},
                        {"color": GOOD, "value": 30 * DAY}], no_value="Unknown"),

    row("Probe detail", 10),
    ts("Failures by reason", (0, 11, 12, 8),
        [target(f'sum by (server, status) (gadget_xrootd_status{{{S}, status!="ok"}}) > 0',
                "{{server}} {{status}}")],
        "Failed probes per minute, by reason. refused: nothing listening on the xrootd port; "
        "timeout: no answer within 15 s; connect_error: no route, or the name doesn't resolve; "
        "tls_error: the TLS handshake failed; protocol_error: something answered, but not as an "
        "xrootd server should (or it refused the login); error: anything else. Empty is good.",
        "short", {"drawStyle": "points", "pointSize": 6}, decimals=0, noValue="No failures"),
    ts("Probe time", (12, 11, 12, 8),
        [target(f'max by (server, stage) (gadget_xrootd_seconds{{{S}}})', "{{server}} {{stage}}")],
        "How long each stage of the probe took. connect: TCP connect; handshake: the xrootd "
        "handshake and protocol request; tls: switching the session to TLS; login: the login "
        "request until the server answers; total: the whole probe, also recorded up to the "
        "failure when a probe fails (a timeout shows as 15 s).", "s"),
]

dash = {
    "uid": "gadget-xrootd",
    "title": "Xrootd",
    "description": "xrootd servers in config/xrootd.toml, probed from the monitoring VM every minute.",
    "tags": ["gadget-monitor"],
    "schemaVersion": 41,
    "editable": False,
    "graphTooltip": 1,
    "refresh": "1m",
    "time": {"from": "now-24h", "to": "now"},
    "timepicker": {},
    "timezone": "browser",
    "templating": {"list": [
        {"name": "server", "label": "Server", "type": "query", "datasource": DS,
         "query": {"query": "label_values(gadget_xrootd_up, server)", "refId": "server"},
         "definition": "label_values(gadget_xrootd_up, server)",
         "refresh": 2, "multi": True, "includeAll": True, "allValue": ".*",
         "current": {"selected": True, "text": ["All"], "value": ["$__all"]}, "sort": 1}]},
    "panels": panels,
}
json.dump(dash, sys.stdout, indent=2)
print()
