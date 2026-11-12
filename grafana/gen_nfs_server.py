#!/usr/bin/env python3
"""Generates grafana/dashboards/nfs-server.json (the source of truth for it).

    python3 grafana/gen_nfs_server.py > grafana/dashboards/nfs-server.json

shire as an NFS server (issue #37): what nfsd is doing, the delegations it has
handed out, its clients, and the two filesystems it exports, kept apart because
they perform very differently: shire (/data, a 1.4 PB MegaRAID volume) and
shiren (/nvme, RAID0 over two NVMe drives). Everything comes from checks that
run on shire as an unprivileged user (checks/nfsd*.sh, checks/nfs_disks.sh,
checks/rdma.sh); the per-client TEST_STATEID and DELEGRETURN panels also use
the client-side nfs check on arnor and gondor.
"""
import json, sys

DS = {"type": "prometheus", "uid": "victoriametrics"}
GOOD, WARN, CRIT = "#0ca30c", "#fab219", "#d03b3b"
S = 'host="shire"'
R = "[$__rate_interval]"
LEGEND = {"displayMode": "list", "placement": "bottom", "showLegend": True}
FS = ("shire", "shiren")
# Fixed colors where a name recurs across panels (dataviz categorical slots).
# Hosts as on Hosts overview; the filesystems take slots 4 and 7, which no
# host uses.
COLORS = {"arnor": "#3987e5", "gondor": "#d95926", "other clients": "#8f8e86",
          "shire": "#c98500", "shiren": "#9085e9",
          "TEST_STATEID": "#d95926", "FREE_STATEID": "#199e70", "DELEGRETURN": "#3987e5",
          "OPEN": "#9085e9", "read": "#3987e5", "write": "#d95926"}
pid = 0

def target(expr, legend, ref="A", instant=False):
    return {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref,
            "range": not instant, "instant": instant}

def targets(*pairs):
    return [target(e, l, chr(ord("A") + i)) for i, (e, l) in enumerate(pairs)]

def overrides(*names):
    """Fixed colors for series whose display name starts with one of these."""
    return [{"matcher": {"id": "byRegexp", "options": f"/^{n}( |$)/"},
             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": COLORS[n]}}]}
            for n in names]

def panel(kind, title, grid, targets, desc, unit=None, custom=None, options=None,
          interval="1m", fixed=(), **defaults):
    global pid
    pid += 1
    fc = {"defaults": {**({"unit": unit} if unit else {}), **defaults}, "overrides": overrides(*fixed)}
    if custom is not None:
        fc["defaults"]["custom"] = custom
    # nfsd checks run every minute, nfs_disks, rdma and the clients' nfs check
    # every 5; without this the datasource's 5m minimum step applies.
    return {"id": pid, "type": kind, "title": title, "description": desc, "datasource": DS,
            "gridPos": dict(zip("xywh", grid)), "targets": targets, "fieldConfig": fc,
            "options": options or {}, "interval": interval}

def row(title, y):
    global pid
    pid += 1
    return {"id": pid, "type": "row", "title": title, "collapsed": False, "panels": [],
            "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}}

def ts(title, grid, targets, desc, unit, custom=None, interval="1m", fixed=(), **defaults):
    return panel("timeseries", title, grid, targets, desc, unit,
                 {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 0, "showPoints": "never",
                  # Bridge one late sample, show any real gap.
                  "spanNulls": 90000 if interval == "1m" else 360000, "axisSoftMin": 0,
                  **(custom or {})},
                 {"legend": LEGEND, "tooltip": {"mode": "multi", "sort": "desc"}},
                 interval, fixed, color={"mode": "palette-classic"}, **defaults)

def stat(title, grid, targets, desc, unit, steps, decimals=None):
    d = {"color": {"mode": "thresholds"}, "thresholds": {"mode": "absolute", "steps": steps},
         "noValue": "No data"}
    if decimals is not None: d["decimals"] = decimals
    return panel("stat", title, grid, targets, desc, unit, None,
                 {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                  "colorMode": "value", "graphMode": "none", "textMode": "value_and_name",
                  "justifyMode": "auto", "orientation": "horizontal"}, **d)

def ops(op, sel=S):
    return f'sum(rate(gadget_nfsd_ops_total{{{sel}, op="{op}"}}{R}))'

def by_client(op):
    """Clients' own rate of op to shire and shiren, plus what the server saw beyond them."""
    client = f'gadget_nfs_ops_total{{fs=~"shire|shiren", op="{op}"}}'
    return targets(
        (f'sum by (host) (rate({client}{R}))', "{{host}}"),
        (f'clamp_min({ops(op)} - (sum(rate({client}{R})) or vector(0)), 0)', "other clients"))

LOG = {"scaleDistribution": {"type": "log", "log": 10}}
NO_STEPS = [{"color": "text", "value": None}]
USED_STEPS = [{"color": GOOD, "value": None}, {"color": WARN, "value": 0.8}, {"color": CRIT, "value": 0.9}]

panels = []
y = 0
panels.append(row("Now", y)); y += 1
panels += [
    stat("Requests", (0, y, 3, 4), [target(f'sum(rate(gadget_nfsd_rpc_calls_total{{{S}}}[5m]))', "per second", instant=True)],
         "NFS requests (RPC calls) per second over the last 5 minutes, every transport and client.",
         "reqps", NO_STEPS, 0),
    stat("TEST_STATEID share", (3, y, 3, 4),
         [target(f'100 * sum(increase(gadget_nfsd_ops_total{{{S}, op="TEST_STATEID"}}[5m])) / '
                 f'sum(increase(gadget_nfsd_ops_total{{{S}, op="SEQUENCE"}}[5m]))', "of requests", instant=True)],
         "TEST_STATEID operations as a percent of NFSv4.1 requests (one SEQUENCE each) over the last "
         "5 minutes. A client tests its stateids when the server flags that some of its state was "
         "revoked. Normally near 0; it was 65% averaged since shire last booted (Oct 2026).",
         "percent", [{"color": GOOD, "value": None}, {"color": WARN, "value": 5}, {"color": CRIT, "value": 25}], 0),
    stat("Delegations held", (6, y, 5, 4),
         [target(f'sum by (fs) (gadget_nfsd_delegations{{{S}}})', "{{fs}}", instant=True)],
         "Delegations shire has handed out and not had back, per filesystem (read and write).",
         "short", NO_STEPS, 0),
    stat("Recalls in progress", (11, y, 3, 4),
         [target(f'sum(gadget_nfsd_delegations_breaking{{{S}}})', "now", instant=True)],
         "Delegations shire has asked a client to give back and is still waiting for (sampled every "
         "minute). Usually 0: clients answer quickly. A count that persists means clients are slow "
         "or not answering recalls, which is how delegations end up revoked.",
         "short", [{"color": GOOD, "value": None}, {"color": WARN, "value": 1}], 0),
    stat("Threads in D", (14, y, 3, 4),
         [target(f'gadget_nfsd_threads{{{S}, state="D"}}', "of 256", instant=True)],
         "nfsd threads in uninterruptible sleep, usually waiting on the disks. Requests queue once "
         "all 256 threads are busy.",
         "short", [{"color": GOOD, "value": None}, {"color": WARN, "value": 64}, {"color": CRIT, "value": 192}], 0),
    stat("Space used", (17, y, 4, 4),
         [target(f'1 - node_filesystem_avail_bytes{{{S}}} / node_filesystem_size_bytes{{{S}}}', "{{fs}}", instant=True)],
         "Share of each exported filesystem in use.", "percentunit", USED_STEPS, 0),
    stat("RDMA clients", (21, y, 3, 4),
         [target(f'count(sum by (client) (gadget_nfsd_client_connections{{{S}, transport="rdma"}}) > 0)',
                 "connected", instant=True)],
         "Clients with at least one NFS-over-RDMA connection to shire.", "short", NO_STEPS, 0),
]
y += 4

panels.append(row("Delegations and state management", y)); y += 1
panels += [
    ts("State management operations", (0, y, 12, 9),
       targets(*((ops(o), o) for o in ("TEST_STATEID", "FREE_STATEID", "DELEGRETURN", "OPEN"))),
       "Operations per second at the server, from every client (log scale). A client sends "
       "TEST_STATEID to check its state after the server flags some of it revoked, and "
       "FREE_STATEID to clear revoked state; many tests with few frees is a client looping on "
       "revoked state. DELEGRETURN is a delegation handed back, OPEN for scale.",
       "ops", LOG, fixed=("TEST_STATEID", "FREE_STATEID", "DELEGRETURN", "OPEN")),
    ts("TEST_STATEID share of requests", (12, y, 6, 9),
       [target(f'100 * {ops("TEST_STATEID")} / {ops("SEQUENCE")}', "TEST_STATEID")],
       "TEST_STATEID operations as a percent of NFSv4.1 requests (one SEQUENCE each).",
       "percent", fixed=("TEST_STATEID",)),
    ts("Delegations held", (18, y, 6, 9),
       [target(f'sum by (fs, type) (gadget_nfsd_delegations{{{S}}})', "{{fs}} {{type}}")],
       "Delegations shire has handed out, per filesystem (read solid, write dashed), from "
       "/proc/locks every minute. Which client holds them is root-only.",
       "short", fixed=FS),
]
# Same color per filesystem; write delegations dashed.
panels[-1]["fieldConfig"]["overrides"].append(
    {"matcher": {"id": "byRegexp", "options": "/ write$/"},
     "properties": [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 6]}}]})
y += 9
panels += [
    ts("TEST_STATEID by client", (0, y, 12, 9), by_client("TEST_STATEID"),
       "TEST_STATEID per second to shire and shiren. arnor and gondor from their own NFS client "
       "counters; other clients is the server's total minus those (rivendell, rohan1-3, annwn, "
       "epyc, the service VMs, ...). Per-client state on the server is root-only, so this is as "
       "close as an unprivileged check gets to who is storming. 5-minute resolution (the clients' "
       "check interval).",
       "ops", interval="5m", fixed=("arnor", "gondor", "other clients")),
    ts("DELEGRETURN by client", (12, y, 12, 9), by_client("DELEGRETURN"),
       "Delegations handed back per second to shire and shiren, split the same way as "
       "TEST_STATEID. A sustained high rate is delegation churn.",
       "ops", interval="5m", fixed=("arnor", "gondor", "other clients")),
]
y += 9
panels += [
    ts("Recalls in progress", (0, y, 8, 8),
       [target(f'gadget_nfsd_delegations_breaking{{{S}}}', "{{fs}}")],
       "Delegations being recalled at each sample: the server asked for them back and is waiting "
       "(up to 45 s, lease-break-time) for the client.",
       "short", fixed=FS, decimals=0),
    ts("Files with shared delegations", (8, y, 8, 8),
       [target(f'gadget_nfsd_delegated_files_shared{{{S}}}', "{{fs}}")],
       "Files with read delegations held by more than one client. One write to such a file "
       "recalls every one of them at once.",
       "short", fixed=FS, decimals=0),
    ts("Stale file handles", (16, y, 8, 8),
       [target(f'sum by (client, fs) (rate(gadget_nfsd_client_stale_filehandles_total{{{S}}}{R})) > 0',
               "{{client}} {{fs}}")],
       "Requests per second answered with a stale file handle (the file was removed or the export "
       "changed under the client), per client. Empty is good.",
       "ops"),
]
y += 8

panels.append(row("Load and saturation", y)); y += 1
panels += [
    ts("Operations by type", (0, y, 12, 9),
       [target(f'sum by (op) (rate(gadget_nfsd_ops_total{{{S}, op!~"SEQUENCE|TEST_STATEID"}}{R}))', "{{op}}")],
       "NFSv4 operations per second by type. Leaves out SEQUENCE (one per request) and "
       "TEST_STATEID (on its own panel, it would dwarf the rest) and the file handle bookkeeping "
       "every request carries. OTHER is everything else (SETATTR, RENAME, REMOVE, LOCK, ...). "
       "The kernel counts these for the whole server, not per export.",
       "ops"),
    ts("Data read and written", (12, y, 6, 9),
       targets((f'rate(gadget_nfsd_read_bytes_total{{{S}}}{R})', "read"),
               (f'rate(gadget_nfsd_write_bytes_total{{{S}}}{R})', "write")),
       "Bytes per second clients read from and wrote to shire, both filesystems.",
       "Bps", fixed=("read", "write")),
    ts("Requests by transport", (18, y, 6, 9),
       [target(f'rate(gadget_nfsd_requests_total{{{S}}}{R}) > 0', "{{transport}}")],
       "Requests per second over RDMA and TCP. Most clients mount over RDMA; the service VMs "
       "(including this one) mount over TCP.",
       "reqps", LOG),
]
y += 9
panels += [
    ts("nfsd threads busy", (0, y, 8, 8),
       targets((f'gadget_nfsd_threads{{{S}, state="D"}}', "waiting (D)"),
               (f'gadget_nfsd_threads{{{S}, state="R"}}', "running (R)"),
               (f'gadget_nfsd_threads_configured{{{S}}}', "configured")),
       "nfsd threads at each sample: waiting in D (usually on the disks) or running. Idle threads "
       "are in I. When all are busy, requests wait for a free thread.",
       "short", decimals=0),
    ts("Thread wakeups", (8, y, 8, 8),
       targets((f'rate(gadget_nfsd_packets_arrived_total{{{S}}}{R})', "requests arrived"),
               (f'rate(gadget_nfsd_threads_woken_total{{{S}}}{R})', "threads woken"),
               (f'rate(gadget_nfsd_threads_timedout_total{{{S}}}{R})', "threads timed out")),
       "From /proc/fs/nfsd/pool_stats: requests arriving and nfsd threads woken to handle them, "
       "per second. Timed out is a thread that waited for work and got none (an idle server).",
       "ops", LOG),
    ts("Bad requests", (16, y, 8, 8),
       [target(f'rate(gadget_nfsd_rpc_bad_calls_total{{{S}}}{R})', "bad requests")],
       "Requests nfsd rejected (malformed, failed authentication, or from an unknown client), per "
       "second. Normally 0.",
       "reqps"),
]
y += 9

panels.append(row("Clients", y)); y += 1
for i, (what, metric) in enumerate((("read", "read_bytes"), ("written", "write_bytes"))):
    panels.append(ts(f"Data {what} by client", (12 * i, y, 12, 9),
        [target(f'sum by (client, fs) (rate(gadget_nfsd_client_{metric}_total{{{S}}}{R})) > 0', "{{client}} {{fs}}")],
        f"Bytes per second {what} per client and filesystem, from /proc/fs/nfsd/export_stats. "
        "Each entry restarts from zero when the export cache refreshes it, so a sample is "
        "occasionally missing.",
        "Bps"))
y += 9
panels.append(ts("Connections by client", (0, y, 24, 8),
    [target(f'sum by (client, transport) (gadget_nfsd_client_connections{{{S}}})', "{{client}} {{transport}}")],
    "Open connections to nfsd per client: RDMA (one per connection; arnor and gondor use 8 each) "
    "and TCP to port 2049. A line ending means the client disconnected; a dip and recovery is a "
    "reconnect.",
    "short", decimals=0))
y += 8

panels.append(row("Storage: shire (/data, MegaRAID) and shiren (/nvme, NVMe RAID0)", y)); y += 1
def disk(metric, fs, level="volume"):
    return f'rate(node_disk_{metric}{{{S}, fs="{fs}", level="{level}"}}{R})'
for i, fs in enumerate(FS):
    x = 12 * i
    busy = f'node_disk_io_time_seconds_total{{{S}, fs="{fs}"'
    panels += [
        ts(f"I/O operations: {fs}", (x, y, 12, 8),
           targets((disk("reads_completed_total", fs), "read"), (disk("writes_completed_total", fs), "write")),
           f"Reads and writes per second on {fs}'s block device.", "iops", interval="5m", fixed=("read", "write")),
        ts(f"Throughput: {fs}", (x, y + 8, 12, 8),
           targets((disk("read_bytes_total", fs), "read"), (disk("written_bytes_total", fs), "write")),
           f"Bytes per second read from and written to {fs}'s block device.", "Bps", interval="5m",
           fixed=("read", "write")),
        ts(f"Wait per I/O: {fs}", (x, y + 16, 12, 8),
           targets((f'{disk("read_time_seconds_total", fs)} / {disk("reads_completed_total", fs)}', "read"),
                   (f'{disk("write_time_seconds_total", fs)} / {disk("writes_completed_total", fs)}', "write")),
           f"Average time per read and per write on {fs}'s block device, queueing included.",
           "s", interval="5m", fixed=("read", "write")),
        ts(f"Busy: {fs}", (x, y + 24, 12, 8),
           [target(f'rate({busy}, level="member"}}{R}) or '
                   f'(rate({busy}, level="volume"}}{R}) unless on (fs) {busy}, level="member"}})', "{{device}}")],
           f"Share of time {fs}'s disks had I/O in flight: each NVMe drive for shiren, the one "
           "MegaRAID volume for shire (whose drives the kernel can't see; 100% there doesn't mean "
           "every drive is busy).",
           "percentunit", interval="5m", min=0, max=1),
        ts(f"XFS journal: {fs}", (x, y + 32, 12, 8),
           targets((f'rate(gadget_xfs_log_forces_total{{{S}, fs="{fs}"}}{R})', "log forces"),
                   (f'rate(gadget_xfs_log_force_sleeps_total{{{S}, fs="{fs}"}}{R})', "force sleeps"),
                   (f'rate(gadget_xfs_log_space_waits_total{{{S}, fs="{fs}"}}{R})', "log space waits")),
           "Per second: log forces (waits for the journal to reach disk, from fsync and NFS COMMIT), "
           "the ones that had to sleep, and transactions stalled because the journal was full "
           "(normally 0).",
           "ops", interval="5m"),
        ts(f"Space and inodes used: {fs}", (x, y + 40, 12, 8),
           targets((f'1 - node_filesystem_avail_bytes{{{S}, fs="{fs}"}} / node_filesystem_size_bytes{{{S}, fs="{fs}"}}', "space"),
                   (f'1 - node_filesystem_files_free{{{S}, fs="{fs}"}} / node_filesystem_files{{{S}, fs="{fs}"}}', "inodes")),
           f"Share of {fs}'s space (available to users) and inodes in use.",
           "percentunit", interval="5m", min=0, max=1),
    ]
y += 48

panels.append(row("RDMA link", y)); y += 1
CONGESTION = "np_cnp_sent|rp_cnp_handled|np_ecn_marked_roce_packets|out_of_buffer|duplicate_request"
panels += [
    ts("RDMA errors and retransmissions", (0, y, 12, 9),
       [target(f'sum by (counter) (rate(gadget_rdma_hw_counter_total{{{S}, counter!~"{CONGESTION}"}}{R})) > 0', "{{counter}}")],
       "Events per second from shire's RDMA adapter (mlx5_bond_0), all clients together. "
       "packet_seq_err and out_of_sequence: packets lost or reordered on the way in. "
       "roce_adp_retrans: shire retransmitting. *_cqe_error and *_remote_access_errors: failed "
       "operations. The same counters on arnor and gondor (Hosts overview) show the other end of "
       "each link. Empty means no errors.",
       "cps", interval="5m"),
    ts("RDMA congestion and receive drops", (12, y, 12, 9),
       [target(f'sum by (counter) (rate(gadget_rdma_hw_counter_total{{{S}, counter=~"{CONGESTION}"}}{R})) > 0', "{{counter}}")],
       "Per second: congestion notifications shire sent (np_cnp_sent) and acted on "
       "(rp_cnp_handled), packets that arrived ECN-marked by the switch, requests a client "
       "repeated (duplicate_request), and packets dropped because nfsd hadn't posted a receive "
       "buffer in time (out_of_buffer: shire falling behind). Empty means none.",
       "cps", interval="5m"),
]
y += 9

dash = {
    "uid": "gadget-nfs-server",
    "title": "NFS server (shire)",
    "description": "shire as an NFS server: requests, delegations, clients, and the shire and shiren filesystems.",
    "tags": ["gadget-monitor"],
    "schemaVersion": 41,
    "editable": False,
    "graphTooltip": 1,
    "refresh": "1m",
    "time": {"from": "now-24h", "to": "now"},
    "timepicker": {},
    "timezone": "browser",
    "templating": {"list": []},
    "panels": panels,
}
json.dump(dash, sys.stdout, indent=2)
print()
