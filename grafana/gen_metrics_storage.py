#!/usr/bin/env python3
"""Generates grafana/dashboards/metrics-storage.json (the source of truth for it).

    python3 grafana/gen_metrics_storage.py > grafana/dashboards/metrics-storage.json

Storage for gadget-monitor's own metrics only: the VictoriaMetrics database against
the /local partition it lives on, from the collector's metrics storage probe
(collector/metrics_storage.py), and a projection of where it is heading.

The projection assumes data keeps arriving at today's rate and costs what it
costs now per sample:
  growth/day   = bytes per stored sample * samples ingested over the last day
  steady state = growth/day * (retention + 31 days); VictoriaMetrics drops
                 old data a whole month at a time, so up to a month more than
                 the retention is on disk
  space        = database size + free space on the partition
If the steady state fits in the space, the disk never fills and the dashboard
shows the steady state; otherwise it shows the date the free space runs out.
While the database is younger than the retention, that is the same as
"show the date if it is less than a retention period away".
"""
import json, sys

DS = {"type": "prometheus", "uid": "victoriametrics"}
GOOD, WARN, CRIT = "#0ca30c", "#fab219", "#d03b3b"
LEGEND = {"displayMode": "list", "placement": "bottom", "showLegend": True}
pid = 0

DB = "gadget_vm_data_bytes"
FREE = "gadget_local_disk_free_bytes"
PER_SAMPLE = f"({DB} / gadget_vm_stored_samples)"
ADDED = "gadget_vm_samples_added_total[1d]"
# Not rate(): VictoriaMetrics divides by the whole window, so for the first day
# of the series (a new deploy) it reads low. Divide by the time actually covered.
PER_DAY = f"(increase({ADDED}) / (tlast_over_time({ADDED}) - tfirst_over_time({ADDED})) * 86400)"
GROWTH = f"({PER_SAMPLE} * {PER_DAY})"                          # bytes per day
STEADY = f"({GROWTH} * (gadget_vm_retention_seconds + 31 * 86400) / 86400)"
SPACE = f"({DB} + {FREE})"
FULL_AT = f"(time() + {FREE} / {GROWTH} * 86400)"


def target(expr, legend, ref="A", instant=False):
    return {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref,
            "range": not instant, "instant": instant}

def panel(kind, title, grid, targets, desc, unit=None, overrides=None, custom=None, options=None, **defaults):
    global pid
    pid += 1
    fc = {"defaults": {**({"unit": unit} if unit else {}), **defaults}, "overrides": overrides or []}
    if custom is not None:
        fc["defaults"]["custom"] = custom
    # The probe runs every 5 minutes.
    return {"id": pid, "type": kind, "title": title, "description": desc, "datasource": DS,
            "gridPos": dict(zip("xywh", grid)), "targets": targets, "fieldConfig": fc,
            "options": options or {}, "interval": "5m"}

def ts(title, grid, targets, desc, unit, overrides=None, log=False, **defaults):
    return panel("timeseries", title, grid, targets, desc, unit, overrides,
                 {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 0, "showPoints": "auto",
                  "spanNulls": 900000,
                  **({"scaleDistribution": {"type": "log", "log": 10}} if log else {"axisSoftMin": 0})},
                 {"legend": LEGEND, "tooltip": {"mode": "multi", "sort": "desc"}},
                 color={"mode": "palette-classic"}, **defaults)

def stat(title, grid, targets, desc, unit, overrides=None, color="text", **defaults):
    return panel("stat", title, grid, targets, desc, unit, overrides, None,
                 {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                  "colorMode": "value", "graphMode": "none", "textMode": "value_and_name",
                  "justifyMode": "auto", "orientation": "horizontal"},
                 color={"mode": "fixed", "fixedColor": color}, **defaults)

def fixed(name, color):
    return {"matcher": {"id": "byName", "options": name},
            "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": color}}]}


panels = [
    stat("Projection", (0, 0, 10, 5),
        # Exactly one of these returns a value.
        [target(f"{FULL_AT} * 1000 and ({STEADY} > {SPACE})", "Disk full on", "A", True),
         target(f"{STEADY} and ({STEADY} <= {SPACE})", "Database levels off at", "B", True)],
        "Where the database is heading if data keeps arriving at the rate of the last day. If it "
        "would outgrow the /local partition before old data starts expiring, this shows the date "
        "the disk fills. Otherwise it shows the size the database levels off at once it holds a "
        "full retention period (plus up to a month, as old data is dropped a month at a time).",
        None,
        [{"matcher": {"id": "byFrameRefID", "options": "A"},
          "properties": [{"id": "unit", "value": "time:YYYY-MM-DD"},
                         {"id": "color", "value": {"mode": "fixed", "fixedColor": CRIT}}]},
         {"matcher": {"id": "byFrameRefID", "options": "B"},
          "properties": [{"id": "unit", "value": "bytes"},
                         {"id": "color", "value": {"mode": "fixed", "fixedColor": GOOD}}]}],
        noValue="Not enough data yet"),
    stat("Database size", (10, 0, 4, 5), [target(DB, "now", instant=True)],
        "Everything VictoriaMetrics keeps on disk: samples and the series index.", "bytes"),
    stat("Growth", (14, 0, 4, 5), [target(GROWTH, "per day", instant=True)],
        "Bytes the database grows by per day before old data starts expiring: the samples "
        "ingested over the last day times the current size per stored sample.", "bytes"),
    stat("Free on /local", (18, 0, 3, 5), [target(FREE, "free", instant=True)],
        "Free space on the VM's /local partition, which holds the database and grafana.db.",
        "bytes"),
    stat("Retention", (21, 0, 3, 5),
        [target("gadget_vm_retention_seconds", "kept for", instant=True)],
        "How long VictoriaMetrics keeps data (-retentionPeriod in compose.yaml).", "dtdurations"),
    ts("Database and disk", (0, 5, 24, 10),
        [target(DB, "Database", "A"),
         target("gadget_local_disk_size_bytes", "/local partition size", "B"),
         target(f"gadget_local_disk_size_bytes - {FREE}", "/local used (all files)", "C"),
         target('gadget_storage_limit_bytes{location="local_vm"}', "Backup refuses above", "D"),
         target('gadget_storage_bytes{location="shire_backup"}', "Backups on shire", "E")],
        "The database against the partition it lives on (log scale). '/local used' includes "
        "grafana.db. Above 'Backup refuses above' the backup container stops copying to shire "
        "(the 10 GB shire budget); 'Backups on shire' is the latest and daily copies together.",
        "bytes",
        [fixed("Database", "#3274d9"), fixed("/local partition size", CRIT),
         fixed("/local used (all files)", "#8f8f8f"), fixed("Backup refuses above", WARN),
         fixed("Backups on shire", "#9085e9")],
        log=True),
    ts("Projected size at steady state", (0, 15, 12, 8),
        [target(STEADY, "Database at steady state", "A"), target(SPACE, "Space for the database", "B")],
        "The size the database would level off at, as estimated at each point in time, against "
        "the room it has (its own size plus the free space on /local). The disk fills if the "
        "first line goes above the second.",
        "bytes", [fixed("Database at steady state", "#3274d9"), fixed("Space for the database", CRIT)],
        log=True),
    ts("Ingest", (12, 15, 12, 8),
        [target(PER_DAY, "Samples per day", "A"), target(PER_SAMPLE, "Bytes per stored sample", "B")],
        "Samples written to the database over the preceding day, and what each stored sample "
        "costs on disk (index included). Bytes per sample falls as VictoriaMetrics merges data "
        "into bigger, better-compressed parts, so the projection tends to err high.",
        "short",
        [{"matcher": {"id": "byName", "options": "Bytes per stored sample"},
          "properties": [{"id": "unit", "value": "suffix: B"}, {"id": "decimals", "value": 2},
                         {"id": "custom.axisPlacement", "value": "right"}]}]),
]

dash = {
    "uid": "gadget-metrics-storage",
    "title": "Gadget metrics storage",
    "description": "Size of gadget-monitor's own metrics database against the VM disk it lives on, and where it is heading.",
    "tags": ["gadget-monitor"],
    "schemaVersion": 41,
    "editable": False,
    "graphTooltip": 1,
    "refresh": "5m",
    "time": {"from": "now-30d", "to": "now"},
    "timepicker": {},
    "timezone": "browser",
    "templating": {"list": []},
    "panels": panels,
}
json.dump(dash, sys.stdout, indent=2)
print()
