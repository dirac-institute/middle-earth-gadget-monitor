# Machine monitor

A small container that reports the CPU, memory, disks and load of the VM it
runs on, for the gadget monitor
([middle-earth-gadget-monitor](https://github.com/dirac-institute/middle-earth-gadget-monitor)).
It serves Prometheus text at `http://<vm fqdn>:9110/metrics`; the gadget
monitor polls that every minute and graphs it on its **VM resources**
dashboard.

Nothing in this folder is specific to a service. The same folder goes into
every service repo that follows
[middle-earth-service-example](https://github.com/dirac-institute/middle-earth-service-example),
unchanged.

## Adding it to a service

1. Copy this folder to the top level of the service's repo, as `monitoring/`.
2. Add this to the `services:` in its `compose.yaml`. It relies only on
   `SVC_UID` and `SVC_GID` in `.env`, which every service's `init.sh` writes.

   ```yaml
     # CPU, memory and disks of this VM, for the gadget monitor (monitoring/README.md).
     monitoring:
       build:
         context: monitoring
       image: localhost/machine-monitor:latest
       container_name: machine-monitor
       init: true
       user: "${SVC_UID}:${SVC_GID}"
       volumes:
         - "/:/host:ro,rslave"
       security_opt:
         - label=disable
       ports:
         - "9110:9110"
       restart: unless-stopped
   ```

3. Deploy as usual (`podman compose up -d --build`), then check it answers
   from the VM itself and from another machine:

   ```bash
   curl -s http://$(hostname -f):9110/metrics | grep -E 'load1 |filesystem_size'
   ```

4. In the gadget monitor's repo, add the VM to `config/machines.toml` and
   deploy that (see its README, "VM resources"):

   ```toml
   [[machine]]
   name = "myservice"            # becomes the `machine` label; keep it stable
   address = "myservice.astro.washington.edu"
   ```

That's all: the dashboard lists every VM in the config.

### What the compose settings are for

- **`/:/host:ro,rslave`** gives the container the VM's filesystems, read-only,
  so it can report their size and free space. `rslave` makes filesystems
  mounted later on the VM show up too. Only local disk filesystems (xfs, ext4,
  btrfs, zfs) are ever touched, by `statvfs`; NFS mounts such as
  `/service/shire` are skipped, so a hung NFS server can't make the monitor
  hang. Never add `:z` to this mount: it would relabel the whole VM.
- **`label=disable`** lets it `statvfs` the VM's filesystems on an
  SELinux-enforcing VM.
- **`ports: 9110:9110`** publishes the endpoint on every interface. Podman
  DNATs published ports, and firewalld lets DNAT'd traffic through whatever
  the zone rules say, so nothing needs opening in the firewall, but it also
  means anyone who can reach the VM can read these numbers. They're CPU,
  memory and disk figures only, which was judged acceptable. To restrict who
  can read them, use `network_mode: host` instead of `ports:`, and open
  9110/tcp in firewalld for the monitoring VM only.
- **`user`**: it needs no privileges. It runs as the service account, like
  the service's other containers.
- **`container_name`**: any name that's unique on the VM.

## What it reports

Everything is read fresh on each request from `/proc` and `/sys`, which the
kernel serves from memory (about a millisecond). `/proc/stat`, `meminfo`,
`loadavg` and `diskstats` aren't namespaced, so inside a container they
describe the whole VM.

| Metric | Meaning |
|---|---|
| `machine_cpu_seconds_total{mode}` | CPU time, summed over cores: user, nice, system, idle, iowait, irq, softirq, steal |
| `machine_cpu_count` | Cores |
| `machine_load1`, `machine_load5`, `machine_load15` | Load averages |
| `machine_memory_{total,free,available}_bytes` | RAM; *available* is what can be had without swapping |
| `machine_swap_{total,free}_bytes` | Swap |
| `machine_boot_time_seconds` | When the VM booted (Unix time) |
| `machine_filesystem_{size,avail}_bytes{mountpoint}` | Size of each local filesystem, and the space ordinary users can still use |
| `machine_filesystem_files{,_avail}{mountpoint}` | Inodes, and inodes still free |
| `machine_disk_{reads,writes}_total{device}` | Completed I/O requests per physical disk |
| `machine_disk_{read,written}_bytes_total{device}` | Bytes read and written per physical disk |
| `machine_disk_io_seconds_total{device}` | Time the disk had requests in flight |
| `machine_monitor_source_success{source}` | 1 if that group of metrics could be read this time |
| `machine_monitor_collect_seconds` | How long this response took to put together |

A filesystem mounted several times (bind mounts, as podman makes for its own
storage) is reported once, under its shortest mount point. Disks are the
devices in `/sys/block` backed by hardware, minus removable ones: no
partitions, loop or device-mapper devices.

A typical VM (two filesystems, one disk) serves 37 lines. The gadget monitor
stores each for a year, at one sample a minute: with its own metrics about
the poll, 46 series and roughly 35 MB a year per VM.

## Settings

Environment variables, for testing; the defaults are what the gadget monitor
expects.

| Variable | Default | |
|---|---|---|
| `MACHINE_MONITOR_PORT` | `9110` | Port to listen on |
| `MACHINE_MONITOR_HOST_ROOT` | `/host` | Where the VM's `/` is mounted; `/` to run it outside a container (`python3 machine_monitor.py`) |
