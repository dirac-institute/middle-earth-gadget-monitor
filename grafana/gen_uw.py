#!/usr/bin/env python3
"""Generates grafana/dashboards-uw/uw-status.json (the source of truth for it).

    python3 grafana/gen_uw.py > grafana/dashboards-uw/uw-status.json

A small status page provisioned into the "UW NetID" folder, which the Grafana
entrypoint opens only to users who signed in with a UW NetID (anonymous
viewers never see it). It is a placeholder for whatever should live behind
SSO: any dashboard JSON put in grafana/dashboards-uw/ gets the same gating.
"""
import json, sys

DS = {"type": "prometheus", "uid": "victoriametrics"}
GOOD, CRIT = "#0ca30c", "#d03b3b"
UPDOWN = {"mappings": [{"type": "value", "options": {
              "0": {"text": "DOWN", "color": CRIT}, "1": {"text": "UP", "color": GOOD}}}],
          "thresholds": {"mode": "absolute", "steps": [{"color": CRIT, "value": None},
                                                       {"color": GOOD, "value": 1}]},
          "color": {"mode": "thresholds"}}

def stat(pid, title, grid, expr, legend, desc):
    return {"id": pid, "type": "stat", "title": title, "description": desc, "datasource": DS,
            "gridPos": dict(zip("xywh", grid)),
            "targets": [{"datasource": DS, "expr": expr, "legendFormat": legend, "refId": "A",
                         "instant": True, "range": False}],
            "fieldConfig": {"defaults": UPDOWN, "overrides": []},
            "options": {"colorMode": "background", "graphMode": "none", "textMode": "value_and_name",
                        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}}

panels = [
    {"id": 1, "type": "text", "title": "", "gridPos": {"x": 0, "y": 0, "w": 24, "h": 2},
     "options": {"mode": "markdown", "content": """\
You are signed in with a **UW NetID**. Dashboards in this folder are visible to anyone with a NetID
and hidden from anonymous visitors; everything else on this site is public."""}},
    stat(2, "Hosts reachable over SSH", (0, 2, 12, 5), "max by (host) (gadget_ssh_connected)",
         "{{host}}", "Whether the collector's SSH session to each host is up."),
    stat(3, "Websites", (12, 2, 12, 5), "max by (site) (gadget_website_up)", "{{site}}",
         "Whether each probed website answered with a good status (see the Websites dashboard)."),
]

dash = {
    "uid": "gadget-uw-status", "title": "UW status", "tags": ["gadget", "uw"],
    "timezone": "browser", "schemaVersion": 39, "editable": False, "refresh": "1m",
    "time": {"from": "now-1h", "to": "now"}, "panels": panels,
}
json.dump(dash, sys.stdout, indent=2)
print()
