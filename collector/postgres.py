"""PostgreSQL probe: each server in config/postgres.toml, from this VM.

The servers run the monitoring endpoint from middle-earth-postgres (pg-monitor
on :9187), which does the database-side work: it queries PostgreSQL and the
backup repository in the background and serves the latest results. So the
collector needs no database credentials. Each probe, three things in parallel:

  vm        TCP connect to vm_port (22): any answer, even a refusal, means
            the VM is up
  port      TCP connect to PostgreSQL's port: reachable from the network
  metrics   GET the endpoint, and import what it serves with a server label

Imported lines are filtered the way check output is (ssh_checks): malformed
lines, reserved labels, names outside the allowed prefixes and anything past
MAX_LINES are dropped and counted, so a misbehaving endpoint can't blow the
series budget.
"""

import asyncio
import errno
import logging
import math
import socket
import time
import tomllib
import urllib.error
import urllib.request

from collector import SRV, fmt_labels
from ssh_checks import METRIC_LINE, parse_labels

POSTGRES_FILE = SRV / "config" / "postgres.toml"
POSTGRES_OFFSET_S = 50             # clear of the hosts', websites' and storage probes
POSTGRES_STATUSES = ("ok", "http_error", "timeout", "connect_error", "error")
BASE_PREFIXES = ("pg_", "pgbackrest_", "pgmon_")
MAX_LINES = 1000
MAX_BODY_BYTES = 1 << 20

log = logging.getLogger("collector")


def tcp_probe(address, port, timeout):
    """Returns ("open" | "refused" | "timeout" | "error", seconds)."""
    t0 = time.monotonic()
    try:
        socket.create_connection((address, port), timeout=timeout).close()
        state = "open"
    except ConnectionRefusedError:
        state = "refused"
    except (socket.timeout, TimeoutError):
        state = "timeout"
    except OSError as e:
        # No route / host unreachable come back as errors, not timeouts.
        state = "timeout" if e.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH) else "error"
    return state, time.monotonic() - t0


def fetch_metrics(url, timeout):
    """Returns (status, body text, seconds, detail)."""
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "gadget-monitor"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(MAX_BODY_BYTES).decode(errors="replace")
        return "ok", body, time.monotonic() - t0, ""
    except urllib.error.HTTPError as e:
        status, detail = "http_error", f"HTTP {e.code}"
    except urllib.error.URLError as e:
        if isinstance(e.reason, (socket.timeout, TimeoutError)):
            status = "timeout"
        elif isinstance(e.reason, OSError):
            status = "connect_error"
        else:
            status = "error"
        detail = repr(e.reason)
    except (socket.timeout, TimeoutError):
        status, detail = "timeout", "read timed out"
    except Exception as e:
        status, detail = "error", repr(e)
    return status, "", time.monotonic() - t0, detail


def filter_lines(text, labels, prefixes, ts_ms, max_lines=MAX_LINES):
    """Endpoint output -> (import lines with labels added, dropped count).

    The machines probe imports its endpoints the same way.
    """
    reserved = {"host", "check"} | labels.keys()
    out, dropped = [], 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = METRIC_LINE.match(line)
        own = parse_labels(m.group(2) or "") if m else None
        try:
            float(m.group(3) if m else "")
        except ValueError:
            own = None
        if (own is None or reserved & own.keys()
                or not m.group(1).startswith(prefixes) or len(out) >= max_lines):
            dropped += 1
            continue
        out.append(f"{m.group(1)}{fmt_labels({**labels, **own})} {m.group(3)} {ts_ms}")
    return out, dropped


class PostgresServer:
    def __init__(self, cfg, interval, timeout, offset, writer):
        self.name = cfg["name"]
        self.address = cfg["address"]
        self.port = cfg.get("port", 5432)
        self.vm_port = cfg.get("vm_port", 22)
        self.url = cfg.get("metrics_url", f"http://{self.address}:9187/metrics")
        self.prefixes = BASE_PREFIXES + tuple(cfg.get("prefixes", ()))
        self.interval = interval
        self.timeout = timeout
        self.offset = offset
        self.writer = writer

    async def probe(self):
        return await asyncio.gather(
            asyncio.to_thread(tcp_probe, self.address, self.vm_port, self.timeout),
            asyncio.to_thread(tcp_probe, self.address, self.port, self.timeout),
            asyncio.to_thread(fetch_metrics, self.url, self.timeout))

    async def probe_loop(self):
        """Probe every interval, one probe at a time.

        Each part has its own timeout, but a DNS lookup can't be interrupted;
        if one is still stuck from a previous probe, record a failure rather
        than start another.
        """
        pending = None
        while True:
            now = time.time()
            await asyncio.sleep(math.floor((now - self.offset) / self.interval + 1) * self.interval
                                + self.offset - now)
            ts_ms = int(time.time() * 1000)
            if pending is None or pending.done():
                pending = asyncio.ensure_future(self.probe())
            try:
                (vm, _), (port, port_s), (status, body, scrape_s, detail) = await asyncio.wait_for(
                    asyncio.shield(pending), self.timeout + 5)
            except asyncio.TimeoutError:
                vm, port, port_s, status, body, scrape_s, detail = (
                    "timeout", "timeout", None, "timeout", "", None, "probe stuck")
            except Exception as e:
                vm, port, port_s, status, body, scrape_s, detail = (
                    "error", "error", None, "error", "", None, repr(e))
            lines, dropped = filter_lines(body, {"server": self.name}, self.prefixes, ts_ms)
            w, lb = self.writer, {"server": self.name}
            w.metric("gadget_postgres_vm_up", lb, 1 if vm in ("open", "refused") else 0, ts_ms)
            w.metric("gadget_postgres_port_up", lb, 1 if port == "open" else 0, ts_ms)
            if port == "open":
                w.metric("gadget_postgres_connect_seconds", lb, round(port_s, 4), ts_ms)
            w.metric("gadget_postgres_exporter_up", lb, 1 if status == "ok" else 0, ts_ms)
            for s in POSTGRES_STATUSES:
                w.metric("gadget_postgres_exporter_status", {**lb, "status": s}, 1 if s == status else 0, ts_ms)
            if scrape_s is not None:
                w.metric("gadget_postgres_scrape_seconds", lb, round(scrape_s, 4), ts_ms)
            w.metric("gadget_postgres_lines_emitted", lb, len(lines), ts_ms)
            w.metric("gadget_postgres_lines_dropped", lb, dropped, ts_ms)
            w.add(lines)
            if status != "ok" or port != "open":
                log.warning("postgres %s: vm %s, port %s, metrics %s (%s)",
                            self.name, vm, port, status, detail)
            if dropped:
                log.warning("postgres %s: dropped %d malformed, disallowed or excess lines",
                            self.name, dropped)


def load_postgres(writer):
    if not POSTGRES_FILE.exists():
        return []
    cfg = tomllib.loads(POSTGRES_FILE.read_text())
    interval, timeout = cfg.get("interval", 60), cfg.get("timeout", 15)
    return [PostgresServer(s, interval, timeout, POSTGRES_OFFSET_S + 2 * i, writer)
            for i, s in enumerate(cfg.get("server", []))]
