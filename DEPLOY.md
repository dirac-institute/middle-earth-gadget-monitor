# Deploying gadget-monitor

Target: a Rocky VM on UW DiRAC infrastructure with `/service/shire` mounted
and a separate local partition on `/local`. The VM's reverse proxy handles TLS
and forwards to `127.0.0.1:8080`.

## First deployment

```bash
git clone <this repo> /root/middle-earth-gadget-monitor
cd /root/middle-earth-gadget-monitor
sudo ./init.sh            # see "Seeding local state" below
podman compose up -d --build
```

On the very first deployment there is no backup yet, so `init.sh` stops and
asks for `--fresh`:

```bash
sudo ./init.sh --fresh
```

Then verify:

```bash
podman ps                                   # four gadget-monitor-* containers, Up
podman logs gadget-monitor-collector        # "arnor: agent up ...", "gondor: agent up ..."
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/   # 200
```

Open the site, check that the **Hosts overview** dashboard shows data for
both hosts, and that the **Last backup** tile shows an age under an hour.

## What each piece does

| File | Role |
|---|---|
| `service.conf` | Service account name, uid and gid (astro-gadget). The single source of truth; everything else reads it via `.env` |
| `init.sh` | Host-level setup only: podman, the service account, SSH hardening, `/local` and shire directories, the Grafana admin password, `.env`, and seeding/restoring local state |
| `compose.yaml` | The four containers and their mounts |
| `collector/` | Collector image: `collector.py` (scheduler, SSH sessions, VictoriaMetrics writer) plus the agent, checks and config copied in |
| `agent/agent.py` | Remote agent, sent to each target over the SSH session. Python 3.9, stdlib only |
| `checks/*.sh` | One check per file (see README.md, "Adding a check") |
| `config/hosts.toml` | Hosts to monitor |
| `config/websites.toml` | Websites the collector fetches from this VM (see README.md, "Websites") |
| `config/known_hosts` | Pinned SSH host keys for the targets |
| `grafana/` | Grafana image: provisioned datasource and dashboards, entrypoint that syncs the admin password and locks the Admin folder, dashboard generators (`gen_hosts_overview.py`, `gen_websites.py`, `gen_users.py`, sharing `user_usage.py`) |
| `backup/` | Backup image: `backup-loop.sh` (scheduler), `backup-once.sh`, `restore.sh`, vmbackup/vmrestore binaries |
| `dev-startup.sh` | Local development with Docker |

## Containers

| Container | Image | Data | Network |
|---|---|---|---|
| `gadget-monitor-vm` | `victoriametrics/victoria-metrics` (pinned) | `/local/gadget-monitor/vm` | compose network only |
| `gadget-monitor-grafana` | built from `grafana/` | `/local/gadget-monitor/grafana` | `127.0.0.1:8080` |
| `gadget-monitor-collector` | built from `collector/Dockerfile` | none (the SSH key is copied to its tmpfs at start) | outbound SSH |
| `gadget-monitor-backup` | built from `backup/` | `/local/gadget-monitor`, shire `backup/` | compose network |

All four run as astro-gadget (uid 1734450). That is required to read the
secrets and write the backups on shire, which is exported with
`root_squash`.

## Updating

After pulling changes (new checks, dashboard edits, code changes):

```bash
git pull
sudo ./init.sh                                  # safe to re-run; refreshes .env
podman compose up -d --build --force-recreate
```

**`--force-recreate` matters.** podman-compose rebuilds the images with
`--build` but does not replace running containers whose image changed unless
you also pass `--force-recreate`.

The database and Grafana state live on `/local`, so recreating containers
loses nothing. Restarting the collector reconnects to both hosts within a
second or two. The agents from the old sessions exit when their session
closes.

## Storage and size budget

| What | Where | Bound |
|---|---|---|
| Live database | `/local/gadget-monitor/vm` | 100 days retention; series caps (20k per hour, 50k per day). The backup refuses to copy anything if it grows past 4.5 GiB |
| Grafana state | `/local/gadget-monitor/grafana` | small |
| Backups | `/service/shire/gadget-monitor/backup/{latest,daily}` | 2 × the live database, so under 10 GB |

Today the database is a few MB. Growth comes from the number of distinct
series, not from depth of history (see README.md, "Adding a check"). The
**Storage** tile on the dashboard shows both sizes.

## Backups

The backup container runs `backup-once.sh` every hour at minute 7:

1. It refuses unless `/local/gadget-monitor/.seeded` exists, and refuses if
   the database is over 4.5 GiB.
2. `vmbackup` takes an instant snapshot and copies only the changed files to
   `backup/latest/vm`.
3. `grafana.db` is copied consistently with SQLite `.backup`.
4. `BACKUP_COMPLETE` is written last. It is removed before a copy starts, so
   a half-written copy is never mistaken for a good one.
5. Once a day the same thing goes to `backup/daily/`, which gives one day of
   rollback if the live database is damaged.

A run that is still going (for example on a hung shire) causes the next one
to be skipped, and each run has a 45-minute timeout. Status is reported every
5 minutes as `gadget_backup_*` metrics.

To back up by hand:

```bash
podman exec gadget-monitor-backup /usr/local/bin/backup-once.sh
```

## Seeding local state and restoring

`init.sh` decides what to do based on `/local/gadget-monitor`:

| Local state | Backup on shire | `init.sh` does |
|---|---|---|
| has `.seeded` | any | nothing (normal re-run) |
| empty | `latest/BACKUP_COMPLETE` exists | restores it and writes `.seeded` |
| empty | nothing at all | stops; re-run with `--fresh` for a first deployment |
| empty | something, but `latest` incomplete | stops; try `--restore-from=daily` |
| empty | shire unreachable | stops; never starts fresh by accident |
| has data, no `.seeded` | any | stops; needs a person to look |

`.seeded` is the safety catch. Without it, a new VM that came up empty would
back up an empty database over the good history.

**New VM, or the VM's disk was lost.** Clone the repo and run `init.sh`
(it restores `latest` automatically), then `podman compose up -d --build`.
At most about an hour of data is lost.

**Damaged local database:**

```bash
podman compose down
mv /local/gadget-monitor /local/gadget-monitor.broken
sudo ./init.sh                       # or --restore-from=daily
podman compose up -d
```

## Verifying a deployment

1. `podman ps`: four containers `Up`. Run it again a minute later and check
   the uptimes haven't reset.
2. `podman logs gadget-monitor-collector`: `agent up` for both hosts, with
   no repeated `connect failed`.
3. Dashboard: data for both hosts, every check `OK`, both SSH sessions
   `Connected`. **Websites** shows every site `Up`, or a reason under
   "Failures by reason" that matches what a browser sees.
4. On a target, `ps -u astro-gadget -f` should show exactly one
   `python3 -I -u -c import sys;...` agent.
5. **Last backup** shows under an hour after the first `:07`.
6. `podman logs gadget-monitor-grafana` says `Admin folder restricted to
   admins (anonymous gets 403)`, and logged out, the dashboard list shows
   only the gadget-monitor folder.
7. Reboot the VM. Afterwards `podman ps` shows all four containers again,
   because `podman-restart.service` is enabled by `init.sh`.

## Troubleshooting

| Symptom | Look at |
|---|---|
| A host shows SSH `Down` | `podman logs gadget-monitor-collector`. "Host key verification failed" means the target's host key changed; update `config/known_hosts` once you're sure it's legitimate. "Permission denied" means the key or account on the target. Timeouts are network problems |
| A check is `FAIL` | Open **Collector internals → Check outcome by reason**. `error`: run the check by hand (README.md). `timeout`: the check is too slow, or something it touches is hanging. `skipped_running`: a previous run is stuck and can't be killed, which almost always means hung NFS on the target |
| `ssh_login` slow or `FAIL` | The **Login stages** panel shows where the time goes. Slow `auth` usually means the directory service (LDAP/sssd) or the NFS home directory (`authorized_keys` lives there). Slow `shell` points at the home directory or login scripts. The persistent sessions keep collecting the other checks either way |
| NFS queue wait high | **NFS queue wait by operation** shows which calls are waiting. High WRITE queue wait alone usually means a large write burst (the client is throttling itself); everything rising together points at the server or network. Check **Round-trip time** too. NFS graphs going blank while `nfs` stays OK means no calls finished on that filesystem: idle, or hung |
| Gaps without failures | The collector itself was down: `podman ps`, `podman logs` |
| **Last backup** growing | `podman logs gadget-monitor-backup`. `REFUSED` explains itself; otherwise check that shire is mounted and writable by astro-gadget |
| Grafana log says the Admin folder could not be restricted | The per-user dashboard may be visible to anonymous viewers. Restart Grafana (`podman restart gadget-monitor-grafana`), or as admin open the Admin folder, Permissions, and remove the Viewer and Editor roles |
| Can't log in as admin | The password is re-applied from shire on every Grafana start: `sudo -u astro-gadget cat /service/shire/gadget-monitor/secrets/grafana-admin-password` |

## VM infrastructure notes

- **SELinux** is currently permissive. Local mounts are labelled with `:z`,
  so switching to enforcing should work for those. The shire mounts are NFS
  and can't be relabelled: if enforcing, run `setsebool -P virt_use_nfs 1`.
- **The service account** is in `/etc/passwd` on this VM, and is an LDAP
  account with an NFS home on the targets. The persistent-session design
  exists because of that NFS home (see PLAN.md).
- **If shire hangs while containers are starting** (for example at boot),
  the containers that mount it (grafana, collector, backup) can't start
  until it recovers. Once running, only the backup container touches shire.
