# Secrets

No secret is ever committed to this repo or baked into a container image.
Secrets live on the `/service/shire` NFS share and are bind-mounted read-only
into the containers that need them.

| Secret | Path | Used by | Created by |
|---|---|---|---|
| SSH private key | `/service/shire/astro-gadget-ssh-key.key` | collector | infrastructure |
| Grafana admin password | `/service/shire/gadget-monitor/secrets/grafana-admin-password` | grafana | `init.sh`, if missing |

## Ownership and root_squash

`/service/shire` is exported with `root_squash`. **Root on the VM can't read
it**, because root is mapped to `nobody`. The files are owned by the service
account from `service.conf` (astro-gadget, uid 1734450) with mode 0600, and
every container runs as that uid.

To read or change a secret, become the service account:

```bash
sudo -u astro-gadget cat /service/shire/gadget-monitor/secrets/grafana-admin-password
sudo -u astro-gadget install -m 0600 /path/to/new-password \
    /service/shire/gadget-monitor/secrets/grafana-admin-password
```

## The SSH key

- The key is dedicated to this service. It gives only an unprivileged login
  as astro-gadget on `arnor` and `gondor`, and nothing else.
- The collector reads it once at startup and keeps a copy on its own tmpfs
  (`/dev/shm` inside the container), so reconnects never wait on NFS. The
  copy disappears when the container stops.
- The targets' SSH host keys are pinned in `config/known_hosts`. The
  collector uses `StrictHostKeyChecking=yes`.

To rotate the key, put the new private key in place (as astro-gadget, mode
0600), add the new public key on the targets, then
`podman compose up -d --force-recreate collector`.

## The Grafana admin password

`init.sh` generates a random password the first time. The Grafana container
re-applies it from the file on **every start**, so the file is always
authoritative. That holds even after restoring a `grafana.db` from backup
that has a different password.

To change it, write the new password to the file and run
`podman compose restart grafana`.

Anonymous visitors get read-only access (Viewer). The admin account is for
editing datasources, users and settings.
