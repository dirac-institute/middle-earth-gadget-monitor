# gadget-monitor: implementation plan

A monitoring service for the `arnor` and `gondor` JupyterHub/compute hosts.
Checks run over SSH every few minutes, results go into a time-series
database, and Grafana provides the graphs. Everything runs in containers on
this VM, deployed the same way as the Middle-earth example this repo was
built from: clone, `sudo ./init.sh`, `podman compose up -d --build`.

## Status (2026-10-27)

All six milestones are built and tested on this VM against arnor and gondor.
Where the build differs from the original plan:

- **Host config is `config/hosts.toml`**, not YAML. Python's standard
  library reads TOML, so the collector has no third-party dependencies.
- **The agent exits after 180 s with no input.** Failure testing showed that
  after a network outage, sshd on the target never notices the client is
  gone, so the old agent lingered.
- **Checks due while a host is disconnected wait up to 60 s for a reconnect**
  before being recorded as `unreachable`. Short outages therefore lose no
  data.
- **Grafana and the collector are custom images** with provisioning, checks
  and config baked in, so a deploy is fully described by the repo.
- **`podman compose up` needs `--force-recreate`** to pick up a rebuilt image
  (see DEPLOY.md).
- **`dev-startup.sh` has no fake target.** Without the key, hosts just show
  as unreachable.
- **The dashboard is generated** by `grafana/gen_hosts_overview.py`.
- **Added after the plan: an SSH login probe.** The collector times a fresh
  interactive login to each host by stage, every 5 minutes, backing off to
  every 15 after repeated timeouts. See README.md.
- **Added after the plan: NFS client stats** (`checks/nfs.sh`) for helios,
  shire and shiren: per-operation queue wait and round-trip time from
  `/proc/self/mountstats` counters. An active round-trip probe (timed LOOKUP)
  was considered and deferred; if added, only on shire/shiren, which are not
  automounted.
- **Added after the plan: GPU stats on gondor** (`checks/gpu.sh`, every
  minute), and the `# hosts:` check header that limits a check to some hosts.
- **The VM reboot test has not been run.** It would end the session doing
  the build. `kill -9` of a container was verified to restart it.

## Requirements

1. Run small check scripts on the targets over SSH, opening as few new SSH
   connections as possible. Authenticate with the keypair on
   `/service/shire`.
2. Checks are cheap point-in-time measurements: under a minute, light on
   resources, safe to run on a host that is breaking. They run about every
   5 minutes.
3. Results go into a time-series database whose state normally ends up on
   `/service/shire`.
4. A **10 GB hard limit** on stored data, with at least 3 months of history.
5. Graphs that can be filtered and sliced, using an existing open-source
   frontend rather than a custom one.
6. Plan for 20–100 metrics per host. The first build has three checks.

## Facts established on the hosts (2026-10-26)

| | this VM | arnor | gondor |
|---|---|---|---|
| OS | Rocky (el10) | Rocky 9.8 | Rocky 9.8 |
| CPUs / RAM | 2 / 15 GB | 256 / 1.5 TB | 128 / 790 GB |
| Swap | — | 544 GB | **none** (SwapTotal 0) |
| `python3` | — | 3.9.25 | 3.9.25 |
| `CLK_TCK` | — | 100 | 100 |
| astro-gadget home | none | `/astro/users/astro-gadget` on NFS (helios) | same |

- astro-gadget (uid 1734450, gid 2121734450) is an LDAP account on the
  targets. Its home directory is on NFS, and that can't be changed.
- Key login works: `/service/shire/astro-gadget-ssh-key.key`, mode 0600,
  readable only by astro-gadget because of `root_squash`.
- arnor and gondor present the **same SSH host key** (the server's identity,
  not the login key): ED25519
  `SHA256:zyhxBFDUoeL8wNyXdFmCtsAvRIbJQwSFEgj3YFEISBU`. It's pinned in
  `config/known_hosts` and has been raised with the sysadmin.
- `/service/shire` is a `hard`-mounted NFSv4.2 export of a 1.4 PB shared
  pool. No quota is visible, so we enforce the 10 GB limit ourselves.
- `/local` on this VM is a separate 16 GB XFS partition.
- `timeout`, `flock`, `setsid`, `awk` and `python3` exist on both targets.
  `/proc/{stat,loadavg,meminfo}` are readable without privileges, so the
  first checks need no sudo.

## Architecture

```
            VM reverse proxy (TLS)
                    │
            127.0.0.1:8080
                    │
┌───────────────────┼──────────────────────────── podman compose ─┐
│             ┌─────▼─────┐   PromQL   ┌─────────────────┐        │
│             │  grafana  ├───────────►│ victoriametrics │        │
│             └─────┬─────┘            └──▲───────────▲──┘        │
│                   │                     │ import    │ snapshot  │
│                   │            ┌────────┴──┐   ┌────┴─────┐     │
│                   │            │ collector │   │  backup  │     │
│                   │            └─────┬─────┘   └────┬─────┘     │
└───────────────────┼──────────────────┼──────────────┼───────────┘
     /local/gadget-monitor/grafana     │ 1 persistent  │ /local → /service/shire
     /local/gadget-monitor/vm          │ ssh per host  │ hourly + daily
                                       ▼
                              arnor, gondor: remote agent
                              (python3, shipped over stdin)
```

Four containers, all running as uid 1734450:

| Service | Image | Role |
|---|---|---|
| `victoriametrics` | official `victoriametrics/victoria-metrics`, pinned | Time-series database. Data in `/local`. Listens only on the compose network |
| `grafana` | official `grafana/grafana-oss`, pinned | Frontend. Published on `127.0.0.1:8080` |
| `collector` | built from this repo (Python + OpenSSH client) | Schedules checks, manages the SSH connections, writes samples |
| `backup` | built from this repo (vmbackup/vmrestore binaries + sqlite3) | Hourly and daily backups to shire, size watchdog |

Apache and the hello-world Flask app are removed. Grafana serves HTTP itself,
and the VM's reverse proxy still handles TLS.

### Why these components

- **VictoriaMetrics**, not Prometheus: it accepts pushed samples that carry
  their own timestamps (`/api/v1/import/prometheus`), can cap how many
  distinct series it accepts (`-storage.maxHourlySeries`,
  `-storage.maxDailySeries`), stores data compactly (around 1 byte per
  sample), and has open-source snapshot, backup and restore tools. Grafana
  uses it through its built-in Prometheus datasource.
- **Grafana**: does the slicing and filtering by label, and dashboards can
  live in the repo as files. Anonymous read-only viewing is built in.
- **A custom collector**, not Telegraf, Nagios or Icinga: the hard
  requirement is one long-lived SSH session per host that survives a
  struggling target (see below). The off-the-shelf schedulers start a new
  SSH process for each check.
- **The live database on local disk, not NFS**: `/service/shire` is mounted
  `hard`. A live database there would freeze the whole monitor during a
  shire outage, which is likely to coincide with problems on the targets.
  Prometheus also doesn't support NFS for its storage.

## The collector and the remote agent

### Why one persistent session per host

astro-gadget's home directory on the targets is on NFS. On *every* new SSH
session, including a multiplexed channel over an existing ControlMaster
connection, sshd changes directory into `$HOME` and starts a shell there. If
the home NFS server is unhealthy, every new session hangs. So OpenSSH
multiplexing alone isn't enough.

**Design:** the collector opens **one SSH session per host and keeps it open
indefinitely**. That session runs a small Python remote agent, which stays
running, reads check requests from stdin and writes results to stdout. Once
it's connected, a cycle starts no new SSH session, no new login, and touches
nothing on the home NFS share.

### How the agent gets there

- The SSH command is roughly
  `cd / && exec /usr/bin/python3 -I -u -c '<small bootstrap>'`.
  `-I` keeps Python from reading `~/.local` or environment variables.
- The bootstrap reads a length-prefixed block of agent source from stdin and
  `exec`s it. The agent source lives in this repo (`agent/agent.py`) and is
  sent again on every connect, so nothing is installed on the targets.
- The agent sticks to the standard library and Python 3.9 syntax, to match
  the targets' system `python3`.

### Protocol

Messages are newline-delimited JSON over the SSH session's stdin and stdout.

- The collector sends:
  `{"id": 17, "check": "memory", "script": "...", "timeout": 30}`
- The agent starts the check with `bash -s` (script on stdin) in its own
  process group, with `cwd=/` and a minimal environment.
- It replies with:
  `{"id": 17, "check": "memory", "ts_ms": ..., "rc": 0, "stdout": "...", "stderr": "...", "duration_s": ..., "status": "ok|error|timeout|skipped_running"}`
- `ts_ms` is the target's clock, read just before the check starts. That
  keeps CPU rate calculations accurate when a check starts late.
- **Timeout:** SIGTERM to the process group, then SIGKILL after 5 s.
- **A check still running from the last cycle** (for example a process stuck
  waiting on hung NFS, which no signal can kill) is not started again. The
  agent replies `skipped_running`, so stuck processes don't pile up on a
  failing host.
- **Heartbeat:** the agent answers `{"ping"}` within a few seconds. If a
  request or ping gets no answer within its deadline, the collector marks the
  host down, kills the local `ssh` process and reconnects with backoff. The
  agent exits when stdin closes, so it never outlives its session.

### SSH settings

- Uses the OpenSSH client: `-i /run/secrets/ssh-key`, `BatchMode=yes`,
  `StrictHostKeyChecking=yes` against `config/known_hosts` in the repo, and
  `ServerAliveInterval=15` with `ServerAliveCountMax=4`.
- Connecting has a 60 s timeout, and reconnects back off exponentially up to
  5 minutes.
- The key file is bind-mounted read-only from shire. The container image
  creates the astro-gadget user so `ssh` can resolve its uid.

### Scheduling

- Each host has its own loop, independent of the other host.
- Every check has an interval (default 300 s). Runs are aligned to wall-clock
  multiples of the interval, with a small fixed offset per host.
- All checks due on a host are sent together and run in parallel on the
  agent.

### What the collector writes

- It parses each check's stdout as Prometheus text-format lines, adds
  `host="arnor"` and `check="memory"` labels, attaches the timestamp, and
  POSTs everything for the cycle to VictoriaMetrics in one batch.
- **Validation:** a check may not set `host` or `check` itself, and is
  limited to 500 lines per run. Malformed lines are dropped and counted.

## The check format

A check is one file, `checks/<name>.sh`, with a metadata header:

```bash
#!/bin/bash
# interval: 300
# timeout: 30
# Reads /proc/meminfo once; emits several metrics.
awk '...' /proc/meminfo
```

**Contract:**
- Run unprivileged, print Prometheus text format on stdout, exit 0 on
  success.
- Never use unbounded values as labels: no PIDs, usernames that come and go,
  or session or job IDs.
- Prefer reading `/proc` and `/sys` over running tools.
- Don't touch NFS paths unless the check exists to test them, and give such a
  check a short timeout.

Adding a metric is normally one new file plus a dashboard panel.

### First three checks

Metric names follow `node_exporter` where the meaning is the same, so queries
and community knowledge carry over. Anything else is prefixed `gadget_`.

| Check | Source | Metrics emitted |
|---|---|---|
| `cpu` | first line of `/proc/stat` (all cores combined), `nproc` | `gadget_cpu_seconds_total{mode="user\|nice\|system\|idle\|iowait\|irq\|softirq\|steal"}` (counter, ticks ÷ `CLK_TCK`), `gadget_cpu_count` |
| `load` | `/proc/loadavg` | `node_load1`, `node_load5`, `node_load15` |
| `memory` | `/proc/meminfo` (one read) | `node_memory_MemTotal_bytes`, `node_memory_MemFree_bytes`, `node_memory_MemAvailable_bytes`, `node_memory_SwapTotal_bytes`, `node_memory_SwapFree_bytes` |

- **CPU %** is computed in Grafana from the counters, so it's the true
  average over each window, not a short sample. The dashboard query is:
  `100 * (1 - sum by (host) (rate(gadget_cpu_seconds_total{mode=~"idle|iowait"}[$__rate_interval])) / sum by (host) (rate(gadget_cpu_seconds_total[$__rate_interval])))`.
  The datasource's `timeInterval` is set to `5m`, so `$__rate_interval`
  always covers at least two samples.
- **Swap totals** are included so that gondor's "no swap" (0 of 0) can be
  told apart from "swap full" (0 free of N).
- **The CPU count** lets a dashboard show load per core. Raw load on a
  256-core host is hard to read otherwise.

### Metrics the collector produces about itself

These are written for every host and check, every cycle:

- `gadget_check_success`, `gadget_check_duration_seconds`,
  `gadget_check_status{status}`, `gadget_check_lines_dropped`
- `gadget_ssh_connected`, `gadget_ssh_reconnects_total`,
  `gadget_clock_skew_seconds` (the target's clock minus the collector's)
- `gadget_cycle_duration_seconds`

A missing data point then always comes with a reason you can see.

## Storage, size budget and backups

### Layout

```
/local/gadget-monitor/                 local XFS, owned by astro-gadget
  vm/                                  VictoriaMetrics -storageDataPath
  grafana/                             grafana.db
  .seeded                              written by restore or by --fresh

/service/shire/gadget-monitor/         NFS, written only by astro-gadget
  backup/latest/vm/                    vmbackup copy, updated incrementally (hourly)
  backup/latest/grafana.db
  backup/latest/BACKUP_COMPLETE        timestamp, written last
  backup/daily/...                     same layout, refreshed from latest once a day
  secrets/grafana-admin-password       0600
```

### Size budget (10 GB hard limit on shire)

- **Retention:** `-retentionPeriod=100d`.
- **Expected usage:** about 35 series per host today, rising to a few
  thousand with 100 metrics. Over 100 days that is under 100 MB at around
  1 byte per sample. Depth of history isn't the risk; the number of distinct
  series is.
- **Series caps:** `-storage.maxHourlySeries=20000` and
  `-storage.maxDailySeries=50000`, so a check that misbehaves can't grow the
  database without limit.
- **The live database is held to 4.5 GB.** latest + daily ≤ 9 GB, plus
  grafana.db, stays under 10 GB.
- **Watchdog:** the backup container measures `/local/gadget-monitor/vm`
  before each run and **refuses to copy anything to shire** if the database
  is over 4.5 GB. It writes `gadget_storage_bytes{location}` and
  `gadget_backup_refused_oversize` metrics, which show on a dashboard.

### Backup (the `backup` container)

**Hourly:**
1. Take an `flock` and skip if the previous run is still going. Wrap the
   whole run in `timeout 45m`. A hung shire stalls only this container.
2. **Refuse to run unless `/local/gadget-monitor/.seeded` exists.** This
   stops an empty database from overwriting a good backup.
3. Run the size watchdog (see above).
4. `vmbackup -storageDataPath=/local/.../vm
   -snapshot.createURL=http://victoriametrics:8428/snapshot/create
   -dst=fs:///shire/backup/latest/vm`. This takes an instant snapshot,
   copies only the changes, and deletes the snapshot afterwards.
5. `sqlite3 grafana.db ".backup /shire/backup/latest/grafana.db.tmp"`, then
   rename it into place.
6. Write `BACKUP_COMPLETE` and record
   `gadget_backup_last_success_timestamp_seconds{kind="hourly"}`.

**Daily**, after a successful hourly run: copy `latest` to `daily`, with the
same marker-last rule. This gives one day of rollback if the database is
corrupted and the hourly mirror copies the damage.

At most one hour of data can be lost.

### Restore (in `init.sh`, with the stack down)

Runs as astro-gadget's uid, using `podman run --user` with the backup image,
because root can't read shire.

| `/local` state | shire | Action |
|---|---|---|
| has `.seeded` | any | Nothing to do |
| empty | `latest/BACKUP_COMPLETE` exists | `vmrestore` + copy `grafana.db`, write `.seeded` |
| empty | no backup | Stop with an error unless `./init.sh --fresh` |
| empty | unreachable or hangs (60 s timeout) | Stop with an error. Never start fresh silently |
| not empty, no `.seeded` | any | Stop with an error. Needs a person to look |

`./init.sh --restore-from=daily` restores the daily copy instead. To recover
from a corrupt local database: `podman compose down`, move
`/local/gadget-monitor` aside, `./init.sh`, `podman compose up -d`.

## Grafana

- **Anonymous access:** `GF_AUTH_ANONYMOUS_ENABLED=true` with organization
  role `Viewer`.
- **One admin account**, password from
  `GF_SECURITY_ADMIN_PASSWORD__FILE` pointing at the shire secret, mounted
  read-only. If the secret is missing, `init.sh` generates it as
  astro-gadget and tells you where it is.
- **Server settings:** `GF_SERVER_ROOT_URL=https://${SVC_FQDN}/`, sign-up
  disabled, telemetry and update checks disabled.
- **Provisioned from the repo:** the VictoriaMetrics datasource (Prometheus
  type, `timeInterval: 5m`) and an "Hosts overview" dashboard with
  `$host` and `$check` variables, showing:
  - CPU %
  - load 1m and 5m, plus load per core
  - available and total memory
  - free swap (showing "no swap" when there is none)
  - check health
  - backup age
- Dashboards edited in the UI persist through grafana.db backups. Important
  ones should be exported back into `grafana/dashboards/`.

## Repo layout after the change

```
service.conf             astro-gadget identity (done)
init.sh                  + /local dirs, restore logic, grafana secret, --fresh / --restore-from
compose.yaml             victoriametrics, grafana, collector, backup
collector/               Dockerfile, collector.py, requirements
agent/agent.py           remote agent (stdlib, py3.9)
checks/                  cpu.sh, load.sh, memory.sh
config/hosts.toml        arnor, gondor: hostname, user, enabled checks
config/known_hosts       pinned target host keys
backup/                  Dockerfile, backup.sh, restore.sh
grafana/provisioning/    datasources/, dashboards/
grafana/dashboards/      hosts-overview.json
DEPLOY.md SECRETS.md README.md   rewritten for gadget-monitor
dev-startup.sh           runs locally with Docker, state in ./dev-data
```

**Housekeeping** (done as part of the build):
- Remove the hello-world app, Apache and `start.sh`.
- Rename every "example" identifier to `gadget-monitor`.
- Drop the stale SELinux claims from the docs, and the duplicate `start.sh`
  row in the DEPLOY.md table.
- Label local mounts with `:z` (shared: the backup container mounts the same dirs). SELinux is permissive today but may be switched to
  enforcing.

## Milestones

1. **Skeleton:** renames and doc fixes, compose with victoriametrics and
   grafana on `/local`, `init.sh` directories and grafana secret, provisioned
   datasource.
   *Verify:* Grafana loads anonymously at `127.0.0.1:8080` and the admin
   login works.
2. **Backup and restore:** the backup container and init.sh restore logic.
   *Verify:* write synthetic samples, back up, `down`, wipe `/local`, run
   `init.sh`, `up`, and the samples are back. Also check that `--fresh` and
   the error stops behave as in the table, and that the backup refuses to run
   without `.seeded`.
3. **Collector and agent** with the `cpu`, `load` and `memory` checks
   against arnor and gondor.
   *Verify:* data arrives every 5 minutes; `ps` on each target shows one
   `sshd` session and one agent for astro-gadget.
4. **Dashboard:** the "Hosts overview" dashboard.
5. **Failure testing:**
   - a check that runs `sleep 600`, to confirm timeout and kill;
   - a check that blocks forever on a FIFO, to confirm it isn't restarted
     while stuck (`skipped_running`);
   - kill the remote agent and block the network for a while, to confirm
     reconnect with backoff;
   - restart the VM, to confirm everything returns.
6. **Docs:** DEPLOY.md, SECRETS.md, README.md, and "how to add a check".

## Future work (no specific provisions needed)

- A JupyterHub "kernel start latency" check, using an API key stored in
  `secrets/` on shire and mounted read-only into the collector the same way
  as the SSH key.
- Grafana alerting, if wanted later.
- More hosts: add an entry to `config/hosts.toml`.
- Checks that need root: narrow sudo rules on the targets, decided case by
  case.
