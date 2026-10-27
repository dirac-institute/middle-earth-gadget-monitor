"""gadget-monitor collector.

Keeps one persistent SSH session per monitored host, running the remote agent
(agent/agent.py). Checks (checks/*.sh) are sent to the agent on schedule; their
Prometheus-format output is labelled with host and check, timestamped with the
target's clock, and pushed to VictoriaMetrics alongside the collector's own
health metrics. See PLAN.md for why sessions are persistent.
"""

import asyncio
import json
import logging
import math
import os
import re
import shlex
import time
import tomllib
import urllib.request
from dataclasses import dataclass
from pathlib import Path

SRV = Path("/srv")
CHECKS_DIR = SRV / "checks"
AGENT_PATH = SRV / "agent" / "agent.py"
HOSTS_FILE = SRV / "config" / "hosts.toml"
KNOWN_HOSTS = SRV / "config" / "known_hosts"
KEY_SRC = Path("/run/secrets/ssh-key")
KEY_COPY = Path("/dev/shm/gadget-ssh-key")
VM_URL = os.environ["GADGET_VM_URL"]

DEFAULT_INTERVAL_S = 300
DEFAULT_TIMEOUT_S = 30
# Beyond the check timeout the agent needs up to ~10 s to kill and report.
RESPONSE_GRACE_S = 30
CONNECT_TIMEOUT_S = 60
PING_EVERY_S = 60
PING_TIMEOUT_S = 30
HOST_STATUS_EVERY_S = 60
BACKOFF_MIN_S = 5
BACKOFF_MAX_S = 300
HOST_OFFSET_S = 10
MAX_LINES_PER_CHECK = 500
MAX_BUFFERED_LINES = 200_000
STATUSES = ("ok", "error", "timeout", "skipped_running", "unreachable", "lost")
RESERVED_LABELS = {"host", "check"}

# Started by sshd in the user's home directory (on NFS for us). Leave it
# immediately, and run Python isolated so it never looks at ~/.local.
BOOTSTRAP = (
    "import sys;n=int(sys.stdin.buffer.readline());"
    "exec(compile(sys.stdin.buffer.read(n),'gadget-agent','exec'))"
)
REMOTE_CMD = f"cd / && exec /usr/bin/python3 -I -u -c {shlex.quote(BOOTSTRAP)}"

log = logging.getLogger("collector")

METRIC_LINE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)$")
LABEL_PAIR = re.compile(r'\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*"((?:[^"\\]|\\.)*)"\s*(,|$)')


@dataclass(frozen=True)
class Check:
    name: str
    script: str
    interval: int
    timeout: int


def load_checks():
    checks = {}
    for path in sorted(CHECKS_DIR.glob("*.sh")):
        script = path.read_text()
        meta = dict(re.findall(r"^#\s*(interval|timeout):\s*(\d+)\s*$", script, re.M))
        checks[path.stem] = Check(
            name=path.stem,
            script=script,
            interval=int(meta.get("interval", DEFAULT_INTERVAL_S)),
            timeout=int(meta.get("timeout", DEFAULT_TIMEOUT_S)),
        )
    return checks


def fmt_labels(labels):
    inner = ",".join(f'{k}="{v}"' for k, v in labels.items())
    return "{" + inner + "}" if inner else ""


def parse_labels(text):
    """Parse the inside of {...}; returns a dict or None if malformed."""
    labels, pos = {}, 0
    text = text.strip()
    while pos < len(text):
        m = LABEL_PAIR.match(text, pos)
        if not m:
            return None
        labels[m.group(1)] = m.group(2)
        pos = m.end()
    return labels


def parse_check_output(stdout, host, check, ts_ms):
    """Prometheus text lines -> (import lines, dropped count)."""
    out, dropped = [], 0
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = METRIC_LINE.match(line)
        labels = parse_labels(m.group(2) or "") if m else None
        try:
            value = float(m.group(3)) if m else None
        except ValueError:
            value = None
        if labels is None or value is None or RESERVED_LABELS & labels.keys() or len(out) >= MAX_LINES_PER_CHECK:
            dropped += 1
            continue
        all_labels = {"host": host, "check": check, **labels}
        out.append(f"{m.group(1)}{fmt_labels(all_labels)} {m.group(3)} {ts_ms}")
    return out, dropped


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


class AgentLost(Exception):
    pass


class Connection:
    """One SSH session running the remote agent."""

    def __init__(self, host):
        self.host = host
        self.proc = None
        self.pending = {}
        self.next_id = 0
        self.closed = asyncio.Event()
        self.hello = None

    async def open(self):
        h = self.host
        cmd = [
            "ssh", "-T", "-F", "/dev/null",
            "-i", str(KEY_COPY),
            "-o", "IdentitiesOnly=yes",
            "-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={KNOWN_HOSTS}",
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "ConnectTimeout=30",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=4",
            "-o", "LogLevel=ERROR",
            f"{h.user}@{h.address}", REMOTE_CMD,
        ]
        self.proc = await asyncio.create_subprocess_exec(
            *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=8 << 20,
        )
        src = AGENT_PATH.read_bytes()
        self.proc.stdin.write(f"{len(src)}\n".encode() + src)
        try:
            await self.proc.stdin.drain()
            line = await asyncio.wait_for(self.proc.stdout.readline(), CONNECT_TIMEOUT_S)
            self.hello = json.loads(line)
            if self.hello.get("type") != "hello":
                raise ValueError(f"unexpected greeting {line[:200]!r}")
        except Exception as e:
            self.kill()
            try:
                err = await asyncio.wait_for(self.proc.stderr.read(), 5)
            except asyncio.TimeoutError:
                err = b""
            raise AgentLost(err.decode(errors="replace").strip() or f"no hello from agent ({e!r})")
        asyncio.create_task(self._read_stdout())
        asyncio.create_task(self._read_stderr())

    async def _read_stdout(self):
        try:
            while line := await self.proc.stdout.readline():
                try:
                    msg = json.loads(line)
                except ValueError:
                    log.warning("%s: bad line from agent: %r", self.host.name, line[:200])
                    continue
                fut = self.pending.pop(msg.get("id"), None)
                if fut and not fut.done():
                    fut.set_result(msg)
        finally:
            self.kill()

    async def _read_stderr(self):
        while line := await self.proc.stderr.readline():
            log.warning("%s: ssh/agent: %s", self.host.name, line.decode(errors="replace").rstrip())

    def kill(self):
        if self.closed.is_set():
            return
        self.closed.set()
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
        for fut in self.pending.values():
            if not fut.done():
                fut.set_exception(AgentLost("session closed"))
        self.pending.clear()

    async def request(self, msg, timeout):
        if self.closed.is_set():
            raise AgentLost("session closed")
        self.next_id += 1
        msg["id"] = self.next_id
        fut = asyncio.get_running_loop().create_future()
        self.pending[msg["id"]] = fut
        self.proc.stdin.write((json.dumps(msg) + "\n").encode())
        try:
            await self.proc.stdin.drain()
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            # No answer at all means the session (or the agent) is wedged.
            self.kill()
            raise AgentLost(f"no response within {timeout}s")
        except (ConnectionError, BrokenPipeError) as e:
            self.kill()
            raise AgentLost(str(e))


class Host:
    def __init__(self, cfg, user, checks, offset, writer):
        self.name = cfg["name"]
        self.address = cfg["address"]
        self.user = cfg.get("user", user)
        wanted = cfg.get("checks", list(checks))
        missing = set(wanted) - checks.keys()
        if missing:
            raise SystemExit(f"host {self.name}: unknown checks {sorted(missing)}")
        self.checks = [checks[c] for c in wanted]
        self.offset = offset
        self.writer = writer
        self.conn = None
        self.reconnects = 0
        self.skew = None

    def labels(self, **extra):
        return {"host": self.name, **extra}

    async def connection_loop(self):
        backoff = BACKOFF_MIN_S
        while True:
            conn = Connection(self)
            t0 = time.time()
            try:
                await conn.open()
            except (AgentLost, OSError) as e:
                log.warning("%s: connect failed: %s; retrying in %ds", self.name, e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_S)
                continue
            backoff = BACKOFF_MIN_S
            log.info("%s: agent up on %s (remote pid %s) after %.1fs", self.name,
                     conn.hello["hostname"], conn.hello["pid"], time.time() - t0)
            self.conn = conn
            self.emit_status()
            await conn.closed.wait()
            self.conn = None
            self.reconnects += 1
            self.emit_status()
            log.warning("%s: session lost; reconnecting", self.name)
            await asyncio.sleep(BACKOFF_MIN_S)

    async def ping_loop(self):
        while True:
            await asyncio.sleep(PING_EVERY_S)
            conn = self.conn
            if conn:
                try:
                    sent = time.time()
                    pong = await conn.request({"type": "ping"}, PING_TIMEOUT_S)
                    # Remote clock minus ours, measured at the round-trip midpoint.
                    self.skew = pong["ts_ms"] / 1000 - (sent + time.time()) / 2
                except AgentLost as e:
                    log.warning("%s: ping failed: %s", self.name, e)

    def emit_status(self):
        w = self.writer
        w.metric("gadget_ssh_connected", self.labels(), 1 if self.conn else 0)
        w.metric("gadget_ssh_reconnects_total", self.labels(), self.reconnects)
        if self.skew is not None:
            w.metric("gadget_clock_skew_seconds", self.labels(), round(self.skew, 3))

    async def status_loop(self):
        while True:
            self.emit_status()
            await asyncio.sleep(HOST_STATUS_EVERY_S)

    def record(self, check, status, duration=None, dropped=0, emitted=0):
        w, lb = self.writer, self.labels(check=check.name)
        w.metric("gadget_check_success", lb, 1 if status == "ok" else 0)
        for s in STATUSES:
            w.metric("gadget_check_status", {**lb, "status": s}, 1 if s == status else 0)
        if duration is not None:
            w.metric("gadget_check_duration_seconds", lb, duration)
        w.metric("gadget_check_lines_dropped", lb, dropped)
        w.metric("gadget_check_lines_emitted", lb, emitted)

    async def run_check(self, check):
        conn = self.conn
        if conn is None:
            self.record(check, "unreachable")
            return
        try:
            resp = await conn.request(
                {"type": "run", "check": check.name, "script": check.script, "timeout": check.timeout},
                check.timeout + RESPONSE_GRACE_S,
            )
        except AgentLost as e:
            log.warning("%s/%s: lost: %s", self.name, check.name, e)
            self.record(check, "lost")
            return
        status = resp["status"]
        lines, dropped = [], 0
        if status == "ok":
            lines, dropped = parse_check_output(resp.get("stdout", ""), self.name, check.name, resp["ts_ms"])
            self.writer.add(lines)
        else:
            log.warning("%s/%s: %s rc=%s stderr=%r", self.name, check.name, status,
                        resp.get("rc"), (resp.get("stderr") or "")[:300])
        if dropped:
            log.warning("%s/%s: dropped %d malformed or excess lines", self.name, check.name, dropped)
        self.record(check, status, resp.get("duration_s"), dropped, len(lines))

    async def schedule_loop(self):
        def next_due(c, now):
            return math.floor((now - self.offset) / c.interval + 1) * c.interval + self.offset

        due = {c.name: next_due(c, time.time()) for c in self.checks}
        by_name = {c.name: c for c in self.checks}
        # Run everything once right away so a restart doesn't leave a gap.
        await self.run_cycle(self.checks)
        while True:
            await asyncio.sleep(max(0.0, min(due.values()) - time.time()))
            now = time.time()
            ready = [by_name[n] for n, t in due.items() if t <= now]
            for c in ready:
                due[c.name] = next_due(c, now)
            asyncio.create_task(self.run_cycle(ready))

    async def run_cycle(self, checks):
        # Give a fresh connection a moment before the first cycle.
        for _ in range(int(CONNECT_TIMEOUT_S)):
            if self.conn:
                break
            await asyncio.sleep(1)
        t0 = time.monotonic()
        await asyncio.gather(*(self.run_check(c) for c in checks))
        self.writer.metric("gadget_cycle_duration_seconds", self.labels(), round(time.monotonic() - t0, 3))


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
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    checks = load_checks()
    cfg = tomllib.loads(HOSTS_FILE.read_text())
    writer = Writer()
    hosts = [Host(h, cfg["user"], checks, i * HOST_OFFSET_S, writer) for i, h in enumerate(cfg["host"])]
    log.info("checks: %s", ", ".join(f"{c.name}({c.interval}s)" for c in checks.values()))
    log.info("hosts: %s", ", ".join(h.name for h in hosts))
    await install_key()
    tasks = [asyncio.create_task(writer.run())]
    for h in hosts:
        for loop in (h.connection_loop, h.ping_loop, h.status_loop, h.schedule_loop):
            tasks.append(asyncio.create_task(loop()))
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
