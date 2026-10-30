# gadget-monitor: notes for agents

SSH-based monitoring of `arnor` and `gondor`: check scripts run on the targets,
results go to VictoriaMetrics and are graphed in Grafana. Read `README.md`
first (how it works, how to add a check), then `DEPLOY.md` (deploying,
verifying, troubleshooting) and `PLAN.md` (design and the size budget) as
needed.

## The live deployment is on this VM

- The production stack runs from `/root/middle-earth-gadget-monitor` (podman
  compose project `gadget-monitor`, containers `gadget-monitor-{vm,grafana,
  collector,backup}`). Grafana is at `127.0.0.1:8080`, behind the VM's
  reverse proxy at https://gadget.astro.washington.edu/.
- Container names and the project name are fixed, so
  `podman compose up -d --build --force-recreate` from **any** checkout of
  this repo (with a copy of `.env`) replaces the live containers. That is how
  changes are tested: there is no separate test stack.
- `.env` is written by `init.sh` and is not in git. Secrets live on
  `/service/shire`, owned by `astro-gadget` with mode 600; read them with
  `sudo -u astro-gadget` (root is squashed on that NFS share).
- Security here is deliberately light: the service account is low-privilege
  and a breach is low-cost. Prefer simple setups over hardening.

## Testing

There is no test suite. Test against the real thing:

- **A check script:** run it on a target by hand (the `podman exec ... 'bash -s'`
  command in README.md, "Adding a check"). Check the output follows the rules
  listed there (Prometheus text, bounded labels, cheap, exit 0).
- **Dashboards:** edit the generator, never the JSON, then regenerate:
  `cd grafana && python3 gen_hosts_overview.py > dashboards/hosts-overview.json`
  (and `gen_users.py > dashboards-admin/users.json`). Deploy, then check the
  panels through the Grafana API, e.g.
  `curl -s 'http://127.0.0.1:8080/api/dashboards/uid/<uid>'`.
- **Query the database** (port 8428 is not published to the host):
  ```bash
  podman exec gadget-monitor-collector python3 -c "import sys,urllib.request,urllib.parse; print(urllib.request.urlopen('http://victoriametrics:8428/api/v1/query?query='+urllib.parse.quote(sys.argv[1])).read().decode())" 'gadget_check_success'
  ```
  The collector's own metrics (`gadget_check_success`, `gadget_check_status`,
  `gadget_check_lines_dropped`, `gadget_ssh_connected`, ...) show whether a
  new check runs cleanly.
- **The whole stack:** deploy, then walk "Verifying a deployment" in
  DEPLOY.md (steps 1-6; skip the reboot).
- `shellcheck` any shell you change; `python3 -m py_compile` any Python.

### Never deploy these to the live stack for testing

- **`backup/` and `init.sh`.** The backup container rewrites the only off-VM
  copy (`/service/shire/gadget-monitor/backup`) at :07 every hour. Test backup
  changes with a one-off `podman run` of the backup image against scratch
  directories (copies of `/local/gadget-monitor` data, a scratch `/shire`),
  never the real mounts.
- **Image version bumps** (Grafana, VictoriaMetrics, base images). A newer
  version may migrate `grafana.db` or the data on disk so the current one can't
  read it back.

Say in the PR which parts were tested live and which weren't.

## Conventions

- Match the surrounding code: comment density, naming, idiom. Comments explain
  why, not what.
- New checks: follow the rules in README.md, add the check to the "Current
  checks" table, and add panels via the generator. Watch the series count
  (size budget in PLAN.md): every label combination is stored for 100 days.
- Keep docs in step with the code (README, DEPLOY, PLAN status).
- Commit messages: a one-line subject saying what changed, then a short body
  saying how and why (see `git log`).

## GitHub

`gh` is logged in with a fine-grained token on the maintainer's account
(Contents, Issues, Pull requests: read/write). `origin` is HTTPS through that
token; `origin-ssh` is for the maintainer's own pushes.

- **Allowed:** push branches, open PRs, comment on issues and PRs.
- **Don't** (by agreement, not enforcement): close issues or PRs, merge PRs,
  delete branches you didn't create.
- **Can't:** push to, force-push, or delete `main`. A repository ruleset
  requires a PR for every change to `main`. Don't try to get around it.
- The maintainer merges PRs and deploys `main` by hand.
