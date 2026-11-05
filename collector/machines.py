"""Machines probe: CPU, memory and disks of each VM in config/machines.toml.

Each VM runs the machine monitor from monitoring/ (the same container on
every service VM), which serves its numbers at :9110/metrics. Every interval
the collector fetches that and imports it with a `machine` label, filtered the
way the postgres probe filters its endpoints: malformed lines, reserved
labels, names not starting with machine_ and anything past MAX_LINES are
dropped and counted.
"""

import asyncio
import logging
import math
import time
import tomllib

from collector import SRV
from postgres import fetch_metrics, filter_lines

MACHINES_FILE = SRV / "config" / "machines.toml"
MACHINES_OFFSET_S = 20             # clear of the hosts' check cycles (0 and 10)
MACHINE_STATUSES = ("ok", "http_error", "timeout", "connect_error", "error")
PREFIXES = ("machine_",)
MAX_LINES = 500
DEFAULT_PORT = 9110

log = logging.getLogger("collector")


class Machine:
    def __init__(self, cfg, interval, timeout, offset, writer):
        self.name = cfg["name"]
        self.url = cfg.get("metrics_url",
                           f"http://{cfg['address']}:{cfg.get('port', DEFAULT_PORT)}/metrics")
        self.interval = interval
        self.timeout = timeout
        self.offset = offset
        self.writer = writer

    async def probe_loop(self):
        """Probe every interval, one probe at a time (see PostgresServer.probe_loop)."""
        pending = None
        while True:
            now = time.time()
            await asyncio.sleep(math.floor((now - self.offset) / self.interval + 1) * self.interval
                                + self.offset - now)
            ts_ms = int(time.time() * 1000)
            if pending is None or pending.done():
                pending = asyncio.ensure_future(asyncio.to_thread(fetch_metrics, self.url, self.timeout))
            try:
                status, body, scrape_s, detail = await asyncio.wait_for(
                    asyncio.shield(pending), self.timeout + 5)
            except asyncio.TimeoutError:
                status, body, scrape_s, detail = "timeout", "", None, "probe stuck"
            except Exception as e:
                status, body, scrape_s, detail = "error", "", None, repr(e)
            lb = {"machine": self.name}
            lines, dropped = filter_lines(body, lb, PREFIXES, ts_ms, MAX_LINES)
            w = self.writer
            w.metric("gadget_machine_up", lb, 1 if status == "ok" else 0, ts_ms)
            for s in MACHINE_STATUSES:
                w.metric("gadget_machine_status", {**lb, "status": s}, 1 if s == status else 0, ts_ms)
            if scrape_s is not None:
                w.metric("gadget_machine_scrape_seconds", lb, round(scrape_s, 4), ts_ms)
            w.metric("gadget_machine_lines_emitted", lb, len(lines), ts_ms)
            w.metric("gadget_machine_lines_dropped", lb, dropped, ts_ms)
            w.add(lines)
            if status != "ok":
                log.warning("machine %s: %s (%s)", self.name, status, detail)
            if dropped:
                log.warning("machine %s: dropped %d malformed, disallowed or excess lines",
                            self.name, dropped)


def load_machines(writer):
    if not MACHINES_FILE.exists():
        return []
    cfg = tomllib.loads(MACHINES_FILE.read_text())
    interval, timeout = cfg.get("interval", 60), cfg.get("timeout", 10)
    return [Machine(m, interval, timeout, MACHINES_OFFSET_S + i, writer)
            for i, m in enumerate(cfg.get("machine", []))]
