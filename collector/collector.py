"""gadget-monitor collector.

Runs the probes and pushes everything they measure to VictoriaMetrics. Each
kind of probe lives in its own module:

  ssh_checks  scheduled checks (checks/*.sh) over one persistent SSH session
              per host, and that session's health metrics
  ssh_login   times a fresh interactive SSH login to each host
  websites    fetches each URL in config/websites.toml from this VM

This module holds what they share: the VictoriaMetrics writer, the SSH key and
ssh client arguments, and main(), which starts them all.
"""

import asyncio
import logging
import os
import time
import tomllib
import urllib.request
from pathlib import Path

SRV = Path("/srv")
HOSTS_FILE = SRV / "config" / "hosts.toml"
KNOWN_HOSTS = SRV / "config" / "known_hosts"
KEY_SRC = Path("/run/secrets/ssh-key")
KEY_COPY = Path("/dev/shm/gadget-ssh-key")
VM_URL = os.environ["GADGET_VM_URL"]

HOST_OFFSET_S = 10
MAX_BUFFERED_LINES = 200_000

log = logging.getLogger("collector")


def ssh_args(host, *extra):
    """Common ssh client arguments: our key only, pinned host keys, no prompts."""
    return [
        "ssh", "-F", "/dev/null",
        "-i", str(KEY_COPY),
        "-o", "IdentitiesOnly=yes",
        "-o", "BatchMode=yes",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=30",
        *extra,
        f"{host.user}@{host.address}",
    ]


def fmt_labels(labels):
    inner = ",".join(f'{k}="{v}"' for k, v in labels.items())
    return "{" + inner + "}" if inner else ""


class Writer:
    """Buffers import lines and pushes them to VictoriaMetrics, retrying."""

    def __init__(self):
        self.buf = []
        self.wake = asyncio.Event()

    def add(self, lines):
        self.buf.extend(lines)
        if len(self.buf) > MAX_BUFFERED_LINES:
            drop = len(self.buf) - MAX_BUFFERED_LINES
            log.warning("write buffer full; dropping %d oldest lines", drop)
            del self.buf[:drop]
        self.wake.set()

    def metric(self, name, labels, value, ts_ms=None):
        ts_ms = ts_ms or int(time.time() * 1000)
        self.add([f"{name}{fmt_labels(labels)} {value} {ts_ms}"])

    @staticmethod
    def _post(body):
        req = urllib.request.Request(f"{VM_URL}/api/v1/import/prometheus", data=body, method="POST")
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()

    async def run(self):
        while True:
            await self.wake.wait()
            self.wake.clear()
            while self.buf:
                batch = self.buf[:10_000]
                try:
                    await asyncio.to_thread(self._post, ("\n".join(batch) + "\n").encode())
                except Exception as e:
                    log.warning("push to VictoriaMetrics failed (%s); retrying in 10s", e)
                    await asyncio.sleep(10)
                    continue
                del self.buf[:len(batch)]

async def install_key():
    """Copy the key off NFS once, so reconnects never depend on shire."""
    while True:
        try:
            data = await asyncio.wait_for(asyncio.to_thread(KEY_SRC.read_bytes), 30)
            break
        except Exception as e:
            log.error("cannot read SSH key %s (%s); retrying in 60s", KEY_SRC, e)
            await asyncio.sleep(60)
    fd = os.open(KEY_COPY, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


async def main():
    # Imported here, not at the top: they import from this module.
    import ssh_checks
    import ssh_login
    import websites

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    checks = ssh_checks.load_checks()
    cfg = tomllib.loads(HOSTS_FILE.read_text())
    writer = Writer()
    hosts = [ssh_checks.Host(h, cfg["user"], checks, i * HOST_OFFSET_S, writer)
             for i, h in enumerate(cfg["host"])]
    log.info("checks: %s", ", ".join(f"{c.name}({c.interval}s)" for c in checks.values()))
    log.info("hosts: %s", ", ".join(h.name for h in hosts))
    sites = websites.load_websites(writer)
    log.info("websites: %s", ", ".join(s.site for s in sites))
    tasks = [asyncio.create_task(writer.run())]
    # Websites don't need the SSH key; start them before waiting on shire for it.
    tasks += [asyncio.create_task(s.probe_loop()) for s in sites]
    await install_key()
    for h in hosts:
        for loop in (h.connection_loop, h.ping_loop, h.status_loop, h.schedule_loop):
            tasks.append(asyncio.create_task(loop()))
        tasks.append(asyncio.create_task(ssh_login.login_probe_loop(h)))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    # Run as a script, this file is __main__; the probe modules import it as
    # "collector". Import it under that name too, so there is one copy.
    import collector
    asyncio.run(collector.main())
