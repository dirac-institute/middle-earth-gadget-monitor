#!/usr/bin/env python3
"""Generates grafana/dashboards/hosts-overview.json (the source of truth for it).

    python3 grafana/gen_hosts_overview.py > grafana/dashboards/hosts-overview.json

Host colors are fixed per host (categorical slots 1, 2 and 6, dark theme) so a host
keeps its color in every panel; dashed lines are the secondary cue.
"""
import json, sys
import user_usage

DS = {"type": "prometheus", "uid": "victoriametrics"}
HOST_COLORS = {"arnor": "#3987e5", "gondor": "#d95926", "shire": "#008300"}  # dataviz categorical slots 1, 2, 6 (dark)
# Login stages use slots 3-5 so they never borrow a host's color.
STAGE_COLORS = {"connect": "#199e70", "auth": "#c98500", "shell": "#d55181"}
# NFS ops take categorical slots 1-8 in order; OTHER is neutral gray, not a hue.
OP_COLORS = {"READ": "#3987e5", "WRITE": "#d95926", "GETATTR": "#199e70", "LOOKUP": "#c98500",
             "ACCESS": "#d55181", "OPEN": "#008300", "CLOSE": "#9085e9", "DELEGRETURN": "#e66767",
             "OTHER": "#8f8e86"}
# Ops the nfs check reports that the by-operation panels fold back into OTHER:
# there are no slots left, and they have their own panels.
NFS_STATE_OPS = "TEST_STATEID|FREE_STATEID"
# Line style per NFS filesystem when a panel shows host x fs.
FS_STYLES = {"shire": {"fill": "dash", "dash": [10, 6]}, "shiren": {"fill": "dot", "dash": [2, 4]}}
# RDMA counters (checks/rdma.sh) take slots 1-8 in a panel repeated per host.
RDMA_COUNTERS = {"packet_seq_err": "#3987e5", "out_of_sequence": "#d95926",
                 "local_ack_timeout_err": "#199e70", "roce_adp_retrans": "#c98500",
                 "roce_slow_restart_cnps": "#d55181", "req_remote_access_errors": "#008300",
                 "req_cqe_error": "#9085e9", "resp_cqe_error": "#e66767"}
# Kernel log message kinds (checks/kernel_log.sh) take slots 1-8, in a panel
# repeated per host like the RDMA counters.
KLOG_KINDS = {"hung_task": "#3987e5", "lockup": "#d95926", "kernel_bug": "#199e70",
              "hardware_error": "#c98500", "io_error": "#d55181", "nfs_not_responding": "#008300",
              "nfs_recall_any": "#9085e9", "rdma_error": "#e66767"}
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
panels.append(row("CPU and load", y)); y += 1
panels.append(ts("CPU utilization", (0, y, 8, 9),
    [target(f'100 * (1 - sum by (host) (rate(gadget_cpu_seconds_total{{{H}, mode=~"idle|iowait"}}[$__interval])) '
            f'/ sum by (host) (rate(gadget_cpu_seconds_total{{{H}}}[$__interval])))', "{{host}}")],
    "Average busy % across all cores (everything except idle and iowait), averaged over each sample interval.",
    "percent", host_overrides(), 0, 100))
panels.append(ts("Load average", (8, y, 8, 9),
    [target(f'node_load1{{{H}}}', "{{host}} 1m"), target(f'node_load5{{{H}}}', "{{host}} 5m", "B")],
    "1-minute (solid) and 5-minute (dashed) load averages. Compare with core counts: arnor 256, gondor 128, shire 32.",
    "short", host_overrides("5m")))
panels.append(ts("Load per core", (16, y, 8, 9),
    [target(f'node_load1{{{H}}} / on (host) gadget_cpu_count{{{H}}}', "{{host}} 1m"),
     target(f'node_load5{{{H}}} / on (host) gadget_cpu_count{{{H}}}', "{{host}} 5m", "B")],
    "Load average divided by online CPUs; around 1.0 means every core has work queued.",
    "short", host_overrides("5m")))
y += 9

panels.append(row("Memory and swap", y)); y += 1
panels.append(ts("Memory available", (0, y, 8, 9),
    [target(f'node_memory_MemAvailable_bytes{{{H}}}', "{{host}} available"),
     target(f'node_memory_MemTotal_bytes{{{H}}}', "{{host}} total", "B")],
    "Available (solid) is how much memory new work can still use, counting cache the kernel would give back. Total (dashed) is installed RAM.",
    "bytes", host_overrides("total")))
panels.append(ts("Direct reclaim stalls", (8, y, 6, 9),
    [target(f'sum by (host) (rate({{__name__=~"node_vmstat_allocstall_.*", {H}}}[$__interval]))', "{{host}}")],
    "Times per second a memory allocation stalled to reclaim memory itself (allocstall, all "
    "zones, from /proc/vmstat). It slows whatever process is allocating, even while memory "
    "available looks fine. 0 is normal.",
    "cps", host_overrides()))
panels.append(ts("Swap free", (14, y, 6, 9),
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
# Repeated over $login_host (hosts with login probe data), not $host: shire
# has no login probe (login_probe = false in hosts.toml).
stages = panel("timeseries", "Login stages: $login_host", (0, y + 8, 24, 9),
    [target(f'gadget_ssh_login_seconds{{host="$login_host", stage="{st}"}}', st, ref)
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
    extra={"repeat": "login_host", "repeatDirection": "h", "maxPerRow": 2})
panels.append(stages)
y += 17

def per_call(metric, sel, by):
    def r(m):
        r = f'rate({m}{{{sel}}}[$__interval])'
        if by == "op":
            r = f'label_replace({r}, "op", "OTHER", "op", "{NFS_STATE_OPS}")'
        return f'sum by ({by}) ({r})'
    return f'{r(f"gadget_nfs_{metric}_seconds_total")} / {r("gadget_nfs_ops_total")}'

def fs_overrides():
    return host_overrides() + [
        {"matcher": {"id": "byRegexp", "options": f"/ {fs}$/"},
         "properties": [{"id": "custom.lineStyle", "value": style}]}
        for fs, style in FS_STYLES.items()]

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

panels.append(row("Stuck processes, NFS state, RDMA and kernel log", y)); y += 1
panels.append(ts("Processes in D state", (0, y, 8, 8),
    [target(f'gadget_processes{{{H}, state="D"}}', "{{host}}")],
    "Processes in uninterruptible sleep (state D), sampled every minute. These are usually "
    "waiting on I/O, often NFS, and can't be killed until it completes. A few for a moment is "
    "normal; a count that stays up means something is stuck. This was the first visible "
    "symptom of the November 2026 outage (hung Jupyter kernels).",
    "short", host_overrides()))
panels.append(ts("NFS state manager threads", (8, y, 8, 8),
    [target(f'gadget_nfs_state_manager_threads{{{H}}}', "{{host}}")],
    "NFSv4 client kernel threads (\"<server>-manager\") recovering lease or delegation state, "
    "sampled every minute. Normally 0: one appears briefly when a server asks for state back. "
    "Present at sample after sample means recovery is looping, as in the November 2026 outage.",
    "short", host_overrides()))
panels.append(ts("TEST_STATEID share of NFS calls", (16, y, 8, 8),
    [target(f'100 * sum by (host) (rate(gadget_nfs_ops_total{{{H}, op="TEST_STATEID"}}[$__interval])) '
            f'/ sum by (host) (rate(gadget_nfs_ops_total{{{H}}}[$__interval]))', "{{host}}")],
    "TEST_STATEID calls as a percent of all NFS calls to helios, shire and shiren. The client "
    "sends TEST_STATEID to check whether state it holds is still valid after the server "
    "revoked some. Normally near 0; above a few percent is abnormal. In the November 2026 "
    "outage it was 81% of all NFSv4 calls.",
    "percent", host_overrides(), 0))
y += 8
panels.append(ts("TEST_STATEID calls", (0, y, 8, 8),
    [target(f'sum by (host, fs) (rate(gadget_nfs_ops_total{{{H}, op="TEST_STATEID"}}[$__interval]))', "{{host}} {{fs}}")],
    "TEST_STATEID calls per second, per host and NFS filesystem (helios solid, shire dashed, "
    "shiren dotted).",
    "ops", fs_overrides()))
panels.append(ts("DELEGRETURN calls", (8, y, 8, 8),
    [target(f'sum by (host, fs) (rate(gadget_nfs_ops_total{{{H}, op="DELEGRETURN"}}[$__interval]))', "{{host}} {{fs}}")],
    "Delegations handed back to the server, per second, per host and filesystem (helios solid, "
    "shire dashed, shiren dotted). A delegation lets a client cache a file; it returns one when "
    "done or when the server recalls it. A sustained high rate is delegation churn, which "
    "preceded the November 2026 outage. Their queue wait is on 'Queue wait by operation'.",
    "ops", fs_overrides()))
panels.append(panel("state-timeline", "RDMA port state", (16, y, 8, 8),
    [target(f'max by (host, device, port) (node_infiniband_state_id{{{H}}} unless on (host, device, port) '
            f'node_infiniband_physical_state_id{{{H}}} == 3) '
            f'or max by (host, device, port) (node_infiniband_physical_state_id{{{H}}} == 3) * 0 - 1',
            "{{host}} {{device}}")],
    "State of each RDMA (RoCE) port, from /sys/class/infiniband. NFS to shire runs over RoCE. "
    "Disabled (gray) is a port whose link is disabled, normally because it isn't cabled: "
    "mlx5_1 on arnor and gondor. shire has one port, the bond mlx5_bond_0. Any other port that isn't ACTIVE is a problem.",
    None, None, {"lineWidth": 0, "fillOpacity": 80},
    {"showValue": "never", "mergeValues": True, "rowHeight": 0.8,
     "legend": {"showLegend": False}, "tooltip": {"mode": "single"}},
    mappings=[{"type": "value", "options": {
        "4": {"text": "ACTIVE", "color": GOOD, "index": 0},
        "1": {"text": "DOWN", "color": CRIT, "index": 1},
        "2": {"text": "INIT", "color": WARN, "index": 2},
        "3": {"text": "ARMED", "color": WARN, "index": 3},
        "5": {"text": "ACTIVE_DEFER", "color": WARN, "index": 4},
        "-1": {"text": "Disabled", "color": "#8f8e86", "index": 5}}}],
    color={"mode": "thresholds"},
    # Thresholds give the same colors as the mappings: -1 gray, 1 red, 2-3 and
    # 5 yellow, 4 green.
    thresholds={"mode": "absolute", "steps": [
        {"color": "#8f8e86", "value": None}, {"color": CRIT, "value": 0}, {"color": WARN, "value": 2},
        {"color": GOOD, "value": 4}, {"color": WARN, "value": 5}]}))
y += 8
# A counter is drawn over the whole range if it moved anywhere in it, so a lone
# 5-minute burst still gets a line (a single non-null point draws nothing). A
# quiet host gets a "no errors" line, so "No data" only means the check is broken.
# Only the error counters; the congestion ones are on the NFS server dashboard.
rdma = f'gadget_rdma_hw_counter_total{{host="$host", counter=~"{"|".join(RDMA_COUNTERS)}"}}'
moved = f'increase({rdma}[$__range] @ end())'
panels.append(panel("timeseries", "RDMA errors and retransmissions: $host", (0, y, 24, 9),
    [target(f'sum by (counter) (rate({rdma}[$__interval])) and on (counter) (sum by (counter) ({moved}) > 0)',
            "{{counter}}"),
     target(f'sum(0 * {rdma}) unless on () (sum({moved}) > 0)', "no errors", "B")],
    "Events per second from the RDMA adapters' hardware counters, all ports summed; only "
    "counters that moved in the time range are drawn, and a green \"no errors\" line at 0 "
    "when none did. packet_seq_err and out_of_sequence: "
    "packets lost or reordered. local_ack_timeout_err: a peer didn't acknowledge in time. "
    "roce_adp_retrans and roce_slow_restart_cnps: retransmissions and congestion slow-downs. "
    "*_remote_access_errors and *_cqe_error: failed operations. The November 2026 outage "
    "showed hundreds of thousands of sequence errors and retransmissions.",
    "cps",
    [{"matcher": {"id": "byName", "options": c},
      "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": col}}]}
     for c, col in {**RDMA_COUNTERS, "no errors": GOOD}.items()],
    {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 0, "showPoints": "never",
     "spanNulls": 600000, "axisSoftMin": 0, "axisSoftMax": 1},
    {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
     "tooltip": {"mode": "multi", "sort": "desc"}},
    extra={"repeat": "host", "repeatDirection": "h", "maxPerRow": 2}))
y += 9
# Same "moved in the range" rule as the RDMA panel. Bars of messages per
# interval: these are rare, and a rate per second of 0.003 reads badly.
klog = 'gadget_kernel_log_messages_total{host="$host"}'
kmoved = f'increase({klog}[$__range] @ end())'
panels.append(panel("timeseries", "Kernel log messages: $host", (0, y, 24, 8),
    [target(f'sum by (kind) (increase({klog}[$__interval])) and on (kind) (sum by (kind) ({kmoved}) > 0)',
            "{{kind}}"),
     target(f'sum(0 * {klog}) unless on () (sum({kmoved}) > 0)', "no messages", "B")],
    "Kernel log (dmesg) messages of a few known kinds, per interval; only kinds seen in the "
    "time range are drawn, and a green \"no messages\" line at 0 when none were. "
    "hung_task: a process stuck in D state for 2+ minutes. lockup: a CPU stuck in the kernel, "
    "or an RCU stall. kernel_bug: BUG, Oops or WARNING. hardware_error: machine checks and "
    "ECC memory errors. io_error: failed disk I/O. nfs_not_responding: an NFS server stopped "
    "answering. nfs_recall_any: a server asked for delegations back (only logged while NFS "
    "callback debugging is on). rdma_error: failed RDMA operations (QP errors, error CQEs). "
    "OOM kills are not here: they are node_vmstat_oom_kill. Read the messages themselves with "
    "dmesg on the host.",
    "short",
    [{"matcher": {"id": "byName", "options": k},
      "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": col}}]}
     for k, col in KLOG_KINDS.items()]
    # A bar of height 0 is invisible, so "no messages" is a line.
    + [{"matcher": {"id": "byName", "options": "no messages"},
        "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": GOOD}},
                       {"id": "custom.drawStyle", "value": "line"},
                       {"id": "custom.lineWidth", "value": 2},
                       {"id": "custom.spanNulls", "value": 600000}]}],
    {"drawStyle": "bars", "lineWidth": 1, "fillOpacity": 80, "showPoints": "never",
     "stacking": {"mode": "normal", "group": "A"}, "axisSoftMin": 0, "axisSoftMax": 1},
    {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
     "tooltip": {"mode": "multi", "sort": "desc"}},
    decimals=0,
    extra={"repeat": "host", "repeatDirection": "h", "maxPerRow": 3}))
y += 8

panels.append(row("NFS queue wait by operation", y)); y += 1
# Repeated over $nfs_host, not $host: shire is the server, not a client, and
# would only add panels that are always empty.
for i, fs in enumerate(NFS_FS):
    sel = f'host="$nfs_host", fs="{fs}"'
    panels.append(panel("timeseries", f"Queue wait by operation: $nfs_host, {fs}", (0, y + 9 * i, 24, 9),
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
        extra={"repeat": "nfs_host", "repeatDirection": "h", "maxPerRow": 2}))
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
        f"used {user_usage.CPU_CORES}+ CPU cores on average, or held {user_usage.MEM_BYTES / 2**30:g}+ GiB "
        f"of RAM, or kept a GPU {user_usage.GPU_PERCENT}%+ busy, or held "
        f"{user_usage.GPU_MEM_BYTES / 2**30:g}+ GiB of GPU memory. root, jhub-admin, "
        "the monitor's own accounts and system accounts are not counted.",
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

dash = {
    "uid": "gadget-hosts-overview",
    "title": "Hosts overview",
    "description": "arnor, gondor and shire, sampled over SSH every 5 minutes by gadget-monitor.",
    "tags": ["gadget-monitor"],
    "schemaVersion": 41,
    "editable": False,
    "graphTooltip": 1,
    "refresh": "5m",
    "time": {"from": "now-24h", "to": "now"},
    "timepicker": {},
    "timezone": "browser",
    # The collector's own health (check status, SSH sessions, backups) lives on
    # its own dashboard so this landing page shows only the hosts (issue #32).
    "links": [{"type": "link", "title": "Gadget collector internals", "icon": "dashboard",
               "url": "/d/gadget-collector-internals", "keepTime": True, "includeVars": True,
               "tooltip": "Check status, SSH sessions, backups", "targetBlank": False}],
    "templating": {"list": [{
        "name": "host", "label": "Host", "type": "query", "datasource": DS,
        "query": {"query": "label_values(gadget_ssh_connected, host)", "refId": "host"},
        "definition": "label_values(gadget_ssh_connected, host)",
        "refresh": 2, "multi": True, "includeAll": True, "allValue": ".*",
        "current": {"selected": True, "text": ["All"], "value": ["$__all"]}, "sort": 1}, {
        # Hidden: the selected hosts with a login probe.
        "name": "login_host", "label": "Login probe", "type": "query", "datasource": DS, "hide": 2,
        "query": {"query": 'label_values(gadget_ssh_login_seconds{host=~"$host"}, host)', "refId": "login_host"},
        "definition": 'label_values(gadget_ssh_login_seconds{host=~"$host"}, host)',
        "refresh": 2, "multi": True, "includeAll": True,
        "current": {"selected": True, "text": ["All"], "value": ["$__all"]}, "sort": 1}, {
        # Hidden: the selected hosts that are NFS clients (run the nfs check).
        "name": "nfs_host", "label": "NFS client", "type": "query", "datasource": DS, "hide": 2,
        "query": {"query": 'label_values(gadget_nfs_ops_total{host=~"$host"}, host)', "refId": "nfs_host"},
        "definition": 'label_values(gadget_nfs_ops_total{host=~"$host"}, host)',
        "refresh": 2, "multi": True, "includeAll": True,
        "current": {"selected": True, "text": ["All"], "value": ["$__all"]}, "sort": 1}]},
    "panels": panels,
}
json.dump(dash, sys.stdout, indent=2)
print()
