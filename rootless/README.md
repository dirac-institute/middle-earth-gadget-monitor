# Rootless prototype (issue #6)

**Can the stack run as rootless podman under astro-gadget? Yes.** The
unchanged `compose.yaml` plus a small override (`compose.rootless.yaml`)
runs all four containers under astro-gadget's own podman, beside the real
rootful stack. Nothing in the images or scripts had to change.

This directory holds the prototype: how to run it, what it needs from the
host, and what a full move would involve. The real deployment is still
rootful, as described in `../DEPLOY.md`.

## Running the prototype

```bash
sudo -iu astro-gadget git clone https://github.com/dirac-institute/middle-earth-gadget-monitor.git gadget-monitor-rootless/repo
sudo ~astro-gadget/gadget-monitor-rootless/repo/rootless/setup.sh
sudo -iu astro-gadget
cd gadget-monitor-rootless/repo
podman compose -f compose.yaml -f rootless/compose.rootless.yaml up -d --build
```

Grafana is then on `127.0.0.1:18080` (not behind the reverse proxy; use an
SSH tunnel, or `tools/panel_data.py --url http://127.0.0.1:18080`). The admin
password is in `~astro-gadget/gadget-monitor-rootless/shire/secrets/`.

Every podman command for the prototype runs as astro-gadget: `sudo -iu
astro-gadget podman ps`. Root's `podman ps` shows only the rootful stack, and
the same container names don't clash because each user has its own podman.

| Path under `~astro-gadget/gadget-monitor-rootless/` | Instead of |
|---|---|
| `repo/` (owned by astro-gadget, with its own `.env`) | `/root/middle-earth-gadget-monitor` |
| `local/` (seeded from a copy of the real latest backup) | `/local/gadget-monitor` |
| `shire/backup/` (the prototype's backup container writes here) | `/service/shire/gadget-monitor/backup` |
| `shire/secrets/` (its own Grafana admin password) | `/service/shire/gadget-monitor/secrets` |

The SSH key is the real one on `/service/shire`, read in place: astro-gadget
owns it, so the rootless containers can mount it straight from NFS.

The prototype runs a second collector, so the targets see two SSH sessions
and run every check twice. That is cheap (and the user checks skip
astro-gadget, so the real dashboards don't show it), but there's no reason to
leave it running. Stop it when you're done looking:

```bash
sudo -iu astro-gadget sh -c 'cd gadget-monitor-rootless/repo && podman compose -f compose.yaml -f rootless/compose.rootless.yaml down'
```

`down` keeps `local/`, so `up -d` brings it back with its history.

## What it needs from the host

`setup.sh` does these, once, as root. They don't affect the rootful stack.

- **Subordinate uids and gids** for astro-gadget
  (`astro-gadget:3000000000:65536` in `/etc/subuid` and `/etc/subgid`). The
  images have files owned by users other than root (grafana's 472, for one),
  and podman needs ids to map them to. The range sits above every real uid
  and gid here (UW gids reach ~2.1e9), so it can't alias anyone's files.
- **Lingering** (`loginctl enable-linger astro-gadget`), so the user's
  systemd runs without a login: rootless podman needs its runtime directory
  (`/run/user/<uid>`), and containers must survive the last logout.
- **`podman-restart.service` as a user unit** (`systemctl --user enable`), the
  rootless twin of the system unit `init.sh` enables, so `restart:
  unless-stopped` containers come back after a reboot. Lingering starts the
  user's systemd at boot, which starts this unit.

To undo: `loginctl disable-linger astro-gadget`, then
`usermod --del-subuids 3000000000-3000065535 --del-subgids 3000000000-3000065535 astro-gadget`.

## What `compose.rootless.yaml` changes, and why

- **`userns_mode: keep-id` on every service.** Rootless podman maps the
  container's root to astro-gadget and every other container uid to the
  subordinate range. `compose.yaml` runs each container as
  `${SVC_UID}`; without keep-id that uid would land in the subordinate range
  and could not read the bind mounts (owned by the real astro-gadget, mode
  0750/0600). keep-id maps uid 1734450 inside to 1734450 outside, so the
  containers see the same ownership they do under rootful podman.
- **Grafana on `127.0.0.1:18080`**, only so it can run beside the rootful
  stack's 8080.

Containers still share podman-compose's default pod; keep-id works inside it.

## Findings

Tested 2026-11-03 on this VM (Rocky 10.2, podman 5.8.2, podman-compose
1.5.0, crun, pasta networking, overlay storage on local disk):

- All four containers start and stay up. The collector reaches both targets
  over SSH (`agent up on arnor ... after 0.1s`) and its writes land in the
  prototype's VictoriaMetrics. Grafana serves the provisioned dashboards from
  that data.
- The Grafana entrypoint syncs the admin password and locks the Admin folder
  (anonymous gets 403), as it does rootful.
- `restore.sh` (via `setup.sh`) restored the real latest backup, mounted
  read-only, into the prototype's `local/`, and `backup-once.sh` wrote a
  complete `latest` and `daily` into the prototype's `shire/backup`.
- Rootless podman keeps its images in `~astro-gadget/.local/share/containers`
  on `/`: about 3.5 GB for this stack, a second copy beside root's while both
  exist.
- `podman-compose` 1.5 doesn't interpolate `${VAR}` inside a value tagged
  `!override`, hence the literal port in the override.
- Not tested: a reboot (lingering + the user `podman-restart.service` should
  bring it back, as the system unit does for rootful), and serving through the
  reverse proxy.

## What a full move would involve

Not done; listed so the decision can be made with the cost in view.

1. **State can stay where it is.** `/local/gadget-monitor` is already owned by
   astro-gadget, and with keep-id the containers see it exactly as they do
   now, so no copy or chown. Secrets and backups on shire get simpler: the
   containers already read them as astro-gadget, and nothing on the host
   needs root to reach them.
2. **Cut-over:** `podman compose down` as root, then `up -d --build` as
   astro-gadget from a checkout it owns, with the same `.env` and Grafana
   back on 8080 (drop the port override). The reverse proxy doesn't change.
3. **`init.sh`** would add the subordinate ids, lingering and the user
   `podman-restart` unit, and run its podman steps (the restore) as
   astro-gadget. Root is still needed for those, packages and SSH hardening.
4. **keep-id belongs in the deployment, not `compose.yaml`:** rootful podman
   and Docker (`dev-startup.sh`) reject `userns_mode: keep-id`. Keeping it in
   an override file (or `COMPOSE_FILE=compose.yaml:rootless/compose.rootless.yaml`
   in `.env`) keeps `compose.yaml` portable.
5. **Every documented command** that runs `podman` (README.md, DEPLOY.md,
   CLAUDE.md, `tools/`, the nightly wrapper's "live stack before/after"
   check and restore) would run as astro-gadget, via `sudo -iu astro-gadget`.
   That is the biggest day-to-day change.
6. **Cleanup:** root's images and the system `podman-restart.service` can go
   once nothing rootful is left.
