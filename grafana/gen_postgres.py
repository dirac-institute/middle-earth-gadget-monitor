#!/usr/bin/env python3
"""Generates grafana/dashboards/postgres.json (the source of truth for it).

    python3 grafana/gen_postgres.py > grafana/dashboards/postgres.json

PostgreSQL servers probed by the collector (config/postgres.toml). The
gadget_postgres_* metrics come from the collector's own probes; pg_* and
pgbackrest_* are imported from each server's monitoring endpoint (see
middle-earth-postgres, monitor/). Nothing here names a server or database, so
adding a server to the config needs no dashboard change.
"""
import json, sys

DS = {"type": "prometheus", "uid": "victoriametrics"}
GOOD, WARN, CRIT = "#0ca30c", "#fab219", "#d03b3b"
S = 'server=~"$server"'
D = 'server=~"$server", datname=~"$datname"'
LEGEND = {"displayMode": "list", "placement": "bottom", "showLegend": True}
DAY = 86400
pid = 0

def target(expr, legend, ref="A", instant=False):
    return {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref,
            "range": not instant, "instant": instant}

def targets(*pairs):
    return [target(e, l, chr(ord("A") + i)) for i, (e, l) in enumerate(pairs)]

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

def age_stat(title, grid, expr, desc, warn, crit):
    """Time since a timestamp metric; green, then amber past warn, red past crit."""
    return stat(title, grid, [target(f"time() - ({expr})", "{{server}}", instant=True)], desc,
                "dtdurations", [{"color": GOOD, "value": None}, {"color": WARN, "value": warn},
                                {"color": CRIT, "value": crit}], no_value="Never")

def ok_stat(title, grid, expr, desc, ok_text, bad_text, bad_value=0):
    good = 1 - bad_value
    return stat(title, grid, [target(expr, "{{server}}", instant=True)], desc, None,
                [{"color": CRIT if bad_value == 0 else GOOD, "value": None},
                 {"color": GOOD if bad_value == 0 else CRIT, "value": 1}],
                mappings=[{"type": "value", "options": {
                    str(good): {"text": ok_text, "color": GOOD, "index": 0},
                    str(bad_value): {"text": bad_text, "color": CRIT, "index": 1}}}],
                no_value="No data")

R = "[$__rate_interval]"
panels = [
    row("Status", 0),
    panel("state-timeline", "Up", (0, 1, 16, 9),
        targets(
            (f'max by (server) (gadget_postgres_vm_up{{{S}}})', "{{server}} VM"),
            (f'max by (server) (gadget_postgres_port_up{{{S}}})', "{{server}} PostgreSQL port"),
            (f'max by (server) (gadget_postgres_exporter_up{{{S}}})', "{{server}} metrics endpoint"),
            (f'max by (server) (pg_up{{{S}}})', "{{server}} PostgreSQL")),
        "Probed from the monitoring VM every minute. VM: the VM answers on TCP (SSH port). "
        "PostgreSQL port: a TCP connection to PostgreSQL succeeds from the network. Metrics "
        "endpoint: the server's pg-monitor answers. PostgreSQL: pg-monitor could query the "
        "database (only known while the endpoint answers).",
        None, None, {"lineWidth": 0, "fillOpacity": 80},
        {"showValue": "never", "mergeValues": True, "rowHeight": 0.8,
         "legend": {"showLegend": False}, "tooltip": {"mode": "single"}},
        mappings=[{"type": "value", "options": {
            "1": {"text": "Up", "color": GOOD, "index": 0},
            "0": {"text": "Down", "color": CRIT, "index": 1}}}],
        color={"mode": "thresholds"},
        thresholds={"mode": "absolute", "steps": [{"color": CRIT, "value": None}, {"color": GOOD, "value": 1}]}),
    stat("PostgreSQL port reachable, selected range", (16, 1, 8, 4),
        [target(f'avg_over_time(gadget_postgres_port_up{{{S}}}[$__range])', "{{server}}", instant=True)],
        "Share of probes over the selected time range in which PostgreSQL's port accepted a "
        "TCP connection from the monitoring VM.",
        "percentunit", [{"color": CRIT, "value": None}, {"color": WARN, "value": 0.99},
                        {"color": GOOD, "value": 0.999}], decimals=2),
    stat("Data disk free", (16, 5, 8, 5),
        [target(f'max by (server) (pg_data_disk_free_bytes{{{S}}} / pg_data_disk_size_bytes{{{S}}})',
                "{{server}}", instant=True)],
        "Free space on the partition holding the database (/data). It also holds WAL that is "
        "waiting to be archived, which grows if backups to shire fail.",
        "percentunit", [{"color": CRIT, "value": None}, {"color": WARN, "value": 0.1},
                        {"color": GOOD, "value": 0.2}], decimals=1),
    age_stat("Last backup", (0, 10, 4, 4),
        f'max by (server) (pgbackrest_last_backup_timestamp_seconds{{{S}}})',
        "Time since the most recent successful backup of any type finished. Backups run nightly.",
        26 * 3600, 50 * 3600),
    age_stat("Last full backup", (4, 10, 4, 4),
        f'max by (server) (pgbackrest_last_backup_timestamp_seconds{{{S}, type="full"}})',
        "Time since the most recent successful full backup finished. Fulls run weekly.",
        8 * DAY, 15 * DAY),
    ok_stat("Last backup job", (8, 10, 4, 4),
        f'max by (server) (pgbackrest_last_run_success{{{S}, job="backup"}})',
        "Whether the scheduler's most recent backup run succeeded.", "OK", "Failed"),
    ok_stat("WAL archiving", (12, 10, 4, 4),
        f'max by (server) (pg_archiver_last_failed_timestamp_seconds{{{S}}} > bool '
        f'pg_archiver_last_archived_timestamp_seconds{{{S}}})',
        "Failing if PostgreSQL's most recent attempt to copy WAL to the backup repository failed. "
        "WAL archiving is what makes point-in-time recovery possible; while it fails, WAL piles "
        "up on /data.", "OK", "Failing", bad_value=1),
    ok_stat("Last restore test", (16, 10, 4, 4),
        f'max by (server) (pgbackrest_restore_test_last_success{{{S}}})',
        "Result of the most recent automated test restore (monthly): restore the latest backup "
        "to scratch space, replay WAL, check every database.", "Pass", "Fail"),
    age_stat("Restore test passed", (20, 10, 4, 4),
        f'max by (server) (pgbackrest_restore_test_last_pass_timestamp_seconds{{{S}}})',
        "Time since a test restore last passed. They run monthly.", 35 * DAY, 63 * DAY),

    row("Connections and queries", 14),
    ts("Connections by state", (0, 15, 12, 8),
        [target(f'sum by (server, state) (pg_connections{{{S}}})', "{{server}} {{state}}")],
        "Client connections. idle_in_transaction: a session holding a transaction open while "
        "doing nothing, which blocks vacuum and can hold locks.", "short", decimals=0),
    ts("Connections by user", (12, 15, 12, 8),
        [target(f'sum by (server, user) (pg_connections_by_user{{{S}}})', "{{server}} {{user}}")],
        "Client connections per database role. Roles with no connections are not shown.",
        "short", decimals=0),
    ts("Transactions per second", (0, 23, 12, 8),
        [target(f'sum by (server, datname) (rate(pg_database_xact_commit_total{{{D}}}{R}) + '
                f'rate(pg_database_xact_rollback_total{{{D}}}{R}))', "{{server}} {{datname}}")],
        "Committed plus rolled-back transactions.", "short"),
    ts("Rows per second", (12, 23, 12, 8),
        targets(
            (f'sum by (server) (rate(pg_database_tup_returned_total{{{D}}}{R}) + '
             f'rate(pg_database_tup_fetched_total{{{D}}}{R}))', "{{server}} read"),
            (f'sum by (server) (rate(pg_database_tup_inserted_total{{{D}}}{R}) + '
             f'rate(pg_database_tup_updated_total{{{D}}}{R}) + rate(pg_database_tup_deleted_total{{{D}}}{R}))',
             "{{server}} written")),
        "Rows read (scanned or fetched by index) and rows inserted, updated or deleted, over "
        "the selected databases.", "short"),
    ts("Statements per second", (0, 31, 12, 8),
        [target(f'sum by (server, datname) (rate(pg_statements_calls_total{{{D}}}{R}))',
                "{{server}} {{datname}}")],
        "Statements executed (pg_stat_statements). The postgres database includes the "
        "monitoring endpoint's own queries, about 40 a minute.", "short"),
    ts("Mean statement time", (12, 31, 12, 8),
        [target(f'sum by (server, datname) (rate(pg_statements_exec_seconds_total{{{D}}}{R})) / '
                f'sum by (server, datname) (rate(pg_statements_calls_total{{{D}}}{R}))',
                "{{server}} {{datname}}")],
        "Average execution time per statement over each interval. Empty when nothing ran.", "s"),
    ts("Longest running statement, oldest transaction", (0, 39, 12, 8),
        targets(
            (f'max by (server) (pg_longest_query_seconds{{{S}}})', "{{server}} statement"),
            (f'max by (server) (pg_oldest_transaction_seconds{{{S}}})', "{{server}} transaction")),
        "Age of the longest-running active statement and of the oldest open transaction, at "
        "each sample. A transaction left open for hours holds back vacuum.", "s",
        {"axisSoftMax": 60}),
    ts("Waiting on locks, deadlocks", (12, 39, 12, 8),
        targets(
            (f'max by (server) (pg_waiting_on_lock{{{S}}})', "{{server}} waiting on a lock"),
            (f'sum by (server) (increase(pg_database_deadlocks_total{{{D}}}{R}))', "{{server}} deadlocks")),
        "Sessions blocked waiting for a lock at each sample, and deadlocks detected per interval.",
        "short", decimals=0),

    row("Storage", 47),
    ts("Database size", (0, 48, 12, 8),
        [target(f'max by (server, datname) (pg_database_size_bytes{{{D}}})', "{{server}} {{datname}}")],
        "Size on disk of each database (tables, indexes, TOAST).", "bytes"),
    ts("Data disk used", (12, 48, 12, 8),
        targets(
            (f'max by (server) (pg_data_disk_size_bytes{{{S}}} - pg_data_disk_free_bytes{{{S}}})', "{{server}} used"),
            (f'max by (server) (pg_wal_directory_bytes{{{S}}})', "{{server}} WAL (pg_wal)")),
        "Space used on the /data partition (left axis; 'Data disk free' above has the share "
        "left), and how much of it is WAL in pg_wal (right axis), which grows if archiving to "
        "the backup repository falls behind.", "bytes",
        overrides=[{"matcher": {"id": "byRegexp", "options": ".*WAL \\(pg_wal\\)"},
                    "properties": [{"id": "custom.axisPlacement", "value": "right"}]}]),
    ts("WAL written per hour", (0, 56, 12, 8),
        [target(f'sum by (server) (rate(pg_wal_bytes_total{{{S}}}{R})) * 3600', "{{server}}")],
        "Write-ahead log generated. All of it is archived to the backup repository, so this "
        "drives the repository's growth between full backups.", "bytes"),
    ts("Transaction ID age", (12, 56, 12, 8),
        [target(f'max by (server, datname) (pg_database_xid_age{{{D}}})', "{{server}} {{datname}}")],
        "Age of each database's oldest unfrozen transaction ID. Autovacuum freezes at 200 "
        "million; PostgreSQL stops accepting writes near 2 billion. Steady growth past 200 "
        "million means vacuum can't keep up (often a long-open transaction).", "short",
        thresholds={"mode": "absolute", "steps": [{"color": GOOD, "value": None},
                                                  {"color": CRIT, "value": 1_000_000_000}]}),

    row("Backups", 64),
    ts("Backup repository size", (0, 65, 12, 8),
        targets(
            (f'max by (server) (pgbackrest_repo_bytes{{{S}}})', "{{server}} repository"),
            (f'max by (server) (pgbackrest_backups_repo_bytes{{{S}}})', "{{server}} backups (without WAL)"),
            (f'sum by (server) (pg_database_size_bytes{{{S}}})', "{{server}} databases")),
        "Everything in the pgBackRest repository on shire (retained backups plus archived WAL), "
        "next to the total size of the databases. Updated every 5 minutes.", "bytes"),
    ts("Size of the last backup", (12, 65, 12, 8),
        [target(f'max by (server, type) (pgbackrest_last_backup_repo_bytes{{{S}}})', "{{server}} {{type}}")],
        "What the most recent backup of each type added to the repository, compressed. "
        "Incrementals store only changed blocks.", "bytes"),
    ts("Backup duration", (0, 73, 12, 8),
        [target(f'max by (server, type) (pgbackrest_last_backup_duration_seconds{{{S}}})', "{{server}} {{type}}")],
        "How long the most recent backup of each type took.", "s"),
    ts("WAL archiving", (12, 73, 12, 8),
        targets(
            (f'sum by (server) (increase(pg_archiver_archived_total{{{S}}}{R}))', "{{server}} archived"),
            (f'sum by (server) (increase(pg_archiver_failed_total{{{S}}}{R}))', "{{server}} failed")),
        "WAL segments copied to the backup repository per interval, and failed attempts "
        "(PostgreSQL retries failures). A few failures right after a new server's first start, "
        "before the backup repository exists, are expected.", "short", decimals=0),

    row("Replication", 81),
    ts("Replicas and slots", (0, 82, 8, 8),
        targets(
            (f'max by (server) (pg_replication_clients{{{S}}})', "{{server}} replicas connected"),
            (f'sum by (server, active) (pg_replication_slots{{{S}}})', "{{server}} slots, active={{active}}")),
        "Replicas streaming from this server, and its replication slots. An inactive slot "
        "keeps WAL on disk forever and can fill /data.", "short", decimals=0),
    ts("Replication lag", (8, 82, 8, 8),
        targets(
            (f'max by (server) (pg_replication_max_lag_seconds{{{S}}})', "{{server}} max replay lag"),
            (f'max by (server) (pg_replica_replay_delay_seconds{{{S}}})', "{{server}} delay as a replica")),
        "As a primary: the largest replay lag among its replicas. As a replica: time since the "
        "last transaction replayed from the primary (this also grows while the primary is idle).",
        "s", {"axisSoftMax": 60}),
    ts("WAL held for replicas", (16, 82, 8, 8),
        targets(
            (f'max by (server) (pg_replication_slot_max_retained_bytes{{{S}}})', "{{server}} by a slot"),
            (f'max by (server) (pg_replication_max_lag_bytes{{{S}}})', "{{server}} replica behind by")),
        "The most WAL any replication slot is keeping on disk, and how far behind the furthest "
        "replica is, in bytes.", "bytes"),
]

dash = {
    "uid": "gadget-postgres",
    "title": "Postgres",
    "description": "PostgreSQL servers in config/postgres.toml, probed from the monitoring VM every minute.",
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
         "query": {"query": "label_values(gadget_postgres_exporter_up, server)", "refId": "server"},
         "definition": "label_values(gadget_postgres_exporter_up, server)",
         "refresh": 2, "multi": True, "includeAll": True, "allValue": ".*",
         "current": {"selected": True, "text": ["All"], "value": ["$__all"]}, "sort": 1},
        {"name": "datname", "label": "Database", "type": "query", "datasource": DS,
         "query": {"query": 'label_values(pg_database_size_bytes{server=~"$server"}, datname)',
                   "refId": "datname"},
         "definition": 'label_values(pg_database_size_bytes{server=~"$server"}, datname)',
         "refresh": 2, "multi": True, "includeAll": True, "allValue": ".*",
         "current": {"selected": True, "text": ["All"], "value": ["$__all"]}, "sort": 1}]},
    "panels": panels,
}
json.dump(dash, sys.stdout, indent=2)
print()
