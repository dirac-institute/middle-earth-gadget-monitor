#!/usr/bin/env python3
"""Generates grafana/dashboards-admin/users.json (the source of truth for it).

    python3 grafana/gen_users.py > grafana/dashboards-admin/users.json

Per-user usage. It is provisioned into the "Admin" folder, which the Grafana
entrypoint restricts to admins, so anonymous viewers never see it. The
definitions of active and the tallies live in user_usage.py.
"""
import json, sys
import user_usage

DS = {"type": "prometheus", "uid": "victoriametrics"}
SEL = 'host=~"$host"'
U = user_usage

tallies = U.tallies(SEL)
names = list(tallies)
refs = [chr(ord("A") + i) for i in range(len(names))]

table = {
    "id": 1, "type": "table", "title": "Usage by user, over the selected time range",
    "description": "Counts only active 5-minute intervals (see the note below). Summed over the "
                   "selected hosts; peaks are the largest single sample. Click a column to sort.",
    "datasource": DS, "gridPos": {"x": 0, "y": 0, "w": 24, "h": 18},
    "targets": [{"datasource": DS, "expr": tallies[n][0], "refId": r, "instant": True,
                 "range": False, "format": "table", "legendFormat": n}
                for n, r in zip(names, refs)],
    "transformations": [
        {"id": "merge", "options": {}},
        {"id": "organize", "options": {
            "excludeByName": {"Time": True},
            "renameByName": {"user": "User", **{f"Value #{r}": n for n, r in zip(names, refs)}},
            "indexByName": {"user": 0, **{f"Value #{r}": i + 1 for i, r in enumerate(refs)}}}},
    ],
    "fieldConfig": {
        "defaults": {"decimals": 1, "custom": {"align": "auto", "filterable": True}},
        "overrides": [{"matcher": {"id": "byName", "options": n},
                       "properties": [{"id": "unit", "value": tallies[n][1]}]} for n in names],
    },
    "options": {"showHeader": True, "sortBy": [{"displayName": "CPU core-hours", "desc": True}],
                "footer": {"show": True, "reducer": ["sum"],
                           "fields": [n for n in names if not n.startswith("Peak")]}},
}

note = {
    "id": 2, "type": "text", "title": "How this is counted",
    "gridPos": {"x": 0, "y": 18, "w": 24, "h": 7},
    "options": {"mode": "markdown", "content": f"""\
Each user is judged per host per 5-minute interval. An interval is **active** if the user, on average,
used **{U.CPU_CORES}+ CPU cores**, or held **{U.MEM_BYTES / 2**30:g}+ GiB of RAM** (anonymous memory plus swap,
not file cache), or kept a GPU **{U.GPU_PERCENT}%+ busy**, or held **{U.GPU_MEM_BYTES / 2**30:g}+ GiB of GPU memory**.
Every column except the peaks sums active intervals only, so idle sessions add nothing.

- **CPU core-hours**: CPU time used (1 core for 1 hour = 1).
- **Memory / GPU memory GiB-hours**: memory held × time held.
- **GPU-hours**: GPU busy share × time (one GPU fully busy for an hour = 1). Sampled once a minute.

root, jhub-admin, astro-gadget and system accounts are never collected. Thresholds live in
`grafana/user_usage.py`; history is reinterpreted when they change."""},
}

dash = {
    "uid": "gadget-users",
    "title": "Users (admin)",
    "description": "Per-user CPU, memory and GPU usage. Admin only.",
    "tags": ["gadget-monitor"],
    "schemaVersion": 41,
    "editable": False,
    "time": {"from": "now-30d", "to": "now"},
    "timepicker": {},
    "timezone": "browser",
    "templating": {"list": [{
        "name": "host", "label": "Host", "type": "query", "datasource": DS,
        "query": {"query": "label_values(gadget_ssh_connected, host)", "refId": "host"},
        "definition": "label_values(gadget_ssh_connected, host)",
        "refresh": 2, "multi": True, "includeAll": True, "allValue": ".*",
        "current": {"selected": True, "text": ["All"], "value": ["$__all"]}, "sort": 1}]},
    "panels": [table, note],
}
json.dump(dash, sys.stdout, indent=2)
print()
