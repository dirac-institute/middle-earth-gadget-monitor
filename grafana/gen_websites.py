#!/usr/bin/env python3
"""Generates grafana/dashboards/websites.json (the source of truth for it).

    python3 grafana/gen_websites.py > grafana/dashboards/websites.json

Websites probed by the collector (config/websites.toml). Nothing here names a
site: every panel covers whatever sites report, so adding a URL to the config
needs no dashboard change. Sites take Grafana's classic palette for the same
reason; the semantic colors are only for up/down and thresholds.
"""
import json, sys

DS = {"type": "prometheus", "uid": "victoriametrics"}
GOOD, WARN, CRIT = "#0ca30c", "#fab219", "#d03b3b"
# Stages use categorical slots 3-7, as on Hosts overview's login stages.
STAGE_COLORS = {"dns": "#199e70", "connect": "#c98500", "tls": "#d55181", "response": "#008300",
                "body": "#9085e9"}
S = 'site=~"$site"'
LEGEND = {"displayMode": "list", "placement": "bottom", "showLegend": True}
pid = 0

def target(expr, legend, ref="A", **kw):
    return {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref, "range": True, **kw}

def panel(kind, title, grid, targets, desc, unit=None, overrides=None, custom=None, options=None, extra=None, **defaults):
    global pid
    pid += 1
    fc = {"defaults": {**({"unit": unit} if unit else {}), **defaults}, "overrides": overrides or []}
    if custom is not None:
        fc["defaults"]["custom"] = custom
    # Probes run every minute; without this the datasource's 5m minimum step applies.
    p = {"id": pid, "type": kind, "title": title, "description": desc, "datasource": DS,
         "gridPos": dict(zip("xywh", grid)), "targets": targets, "fieldConfig": fc,
         "options": options or {}, "interval": "1m"}
    p.update(extra or {})
    return p

def ts(title, grid, targets, desc, unit, custom=None, overrides=None, **defaults):
    return panel("timeseries", title, grid, targets, desc, unit, overrides,
                 {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 0, "showPoints": "never",
                  "spanNulls": 180000, "axisSoftMin": 0, **(custom or {})},
                 {"legend": LEGEND, "tooltip": {"mode": "multi", "sort": "desc"}},
                 color={"mode": "palette-classic"}, **defaults)

def stat(title, grid, targets, desc, unit, steps, decimals=None):
    d = {"color": {"mode": "thresholds"}, "thresholds": {"mode": "absolute", "steps": steps}}
    if decimals is not None: d["decimals"] = decimals
    return panel("stat", title, grid, targets, desc, unit, None, None,
                 {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                  "colorMode": "value", "graphMode": "none", "textMode": "value_and_name",
                  "justifyMode": "auto", "orientation": "horizontal"}, **d)

panels = [
    panel("state-timeline", "Up", (0, 0, 16, 8),
        [target(f'max by (site) (gadget_website_up{{{S}}})', "{{site}}")],
        "Whether each site answered 200 OK (after up to 5 redirects) within 15 s, with a "
        "certificate that verifies, probed from the monitoring VM every minute. 'Failures by "
        "reason' below says why a probe failed.",
        None, None, {"lineWidth": 0, "fillOpacity": 80},
        {"showValue": "never", "mergeValues": True, "rowHeight": 0.8,
         "legend": {"showLegend": False}, "tooltip": {"mode": "single"}},
        mappings=[{"type": "value", "options": {
            "1": {"text": "Up", "color": GOOD, "index": 0},
            "0": {"text": "Down", "color": CRIT, "index": 1}}}],
        color={"mode": "thresholds"},
        thresholds={"mode": "absolute", "steps": [{"color": CRIT, "value": None}, {"color": GOOD, "value": 1}]}),
    stat("Uptime, selected range", (16, 0, 8, 4),
        [target(f'avg_over_time(gadget_website_up{{{S}}}[$__range])', "{{site}}", instant=True)],
        "Share of probes over the selected time range that found the site up.",
        "percentunit", [{"color": CRIT, "value": None}, {"color": WARN, "value": 0.99},
                        {"color": GOOD, "value": 0.999}], decimals=2),
    stat("Certificate expires in", (16, 4, 8, 4),
        [target(f'max by (site) (gadget_website_cert_expiry_timestamp_seconds{{{S}}}) - time()',
                "{{site}}", instant=True)],
        "Time until the TLS certificate the site served on its last successful handshake expires. "
        "Most certificates renew automatically around 30 days before expiry.",
        "dtdurations", [{"color": CRIT, "value": None}, {"color": WARN, "value": 7 * 86400},
                        {"color": GOOD, "value": 21 * 86400}]),
    ts("Response time", (0, 8, 12, 9),
        [target(f'gadget_website_seconds{{{S}, stage="total"}}', "{{site}}")],
        "Time to fetch the whole page, from DNS lookup to the last byte, including redirects. "
        "A failed probe shows the time until it failed; timeouts show as 15 s.", "s"),
    ts("Failures by reason", (12, 8, 12, 9),
        [target(f'sum by (site, status) (gadget_website_status{{{S}, status!="ok"}}) > 0',
                "{{site}} {{status}}")],
        "Failed probes per minute, by reason. http_error: answered with anything but 200 "
        "(or more than 5 redirects); timeout: no complete answer within 15 s; dns_error, "
        "connect_error, tls_error (including a certificate that doesn't verify): failed at that "
        "step; error: a malformed or broken response. Empty is good.", "short",
        {"drawStyle": "points", "pointSize": 6}, decimals=0),
    ts("HTTP status", (0, 17, 12, 8),
        [target(f'gadget_website_http_status{{{S}}}', "{{site}}")],
        "Status code of the final response (after redirects). 0 means no response at all.",
        "none", decimals=0),
    panel("timeseries", "Response time by stage: $site", (0, 25, 24, 9),
        [target(f'gadget_website_seconds{{site="$site", stage="{st}"}}', st, chr(ord("A") + i))
         for i, st in enumerate(STAGE_COLORS)],
        "Where the response time goes, stacked. dns: name lookup; connect: TCP; tls: handshake and "
        "certificate check; response: request sent until the headers arrive (mostly the server "
        "building the page); body: reading the page. Redirects add their stages to the same totals.",
        "s",
        [{"matcher": {"id": "byName", "options": st},
          "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]}
         for st, c in STAGE_COLORS.items()],
        {"drawStyle": "line", "lineWidth": 1, "fillOpacity": 70, "showPoints": "never",
         "spanNulls": 180000, "axisSoftMin": 0, "stacking": {"mode": "normal", "group": "A"}},
        {"legend": LEGEND, "tooltip": {"mode": "multi", "sort": "none"}},
        extra={"repeat": "site", "repeatDirection": "h", "maxPerRow": 2}),
]

dash = {
    "uid": "gadget-websites",
    "title": "Websites",
    "description": "Websites in config/websites.toml, fetched from the monitoring VM every minute.",
    "tags": ["gadget-monitor"],
    "schemaVersion": 41,
    "editable": False,
    "graphTooltip": 1,
    "refresh": "1m",
    "time": {"from": "now-24h", "to": "now"},
    "timepicker": {},
    "timezone": "browser",
    "templating": {"list": [{
        "name": "site", "label": "Site", "type": "query", "datasource": DS,
        "query": {"query": "label_values(gadget_website_up, site)", "refId": "site"},
        "definition": "label_values(gadget_website_up, site)",
        "refresh": 2, "multi": True, "includeAll": True, "allValue": ".*",
        "current": {"selected": True, "text": ["All"], "value": ["$__all"]}, "sort": 1}]},
    "panels": panels,
}
json.dump(dash, sys.stdout, indent=2)
print()
