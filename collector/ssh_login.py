"""SSH login probe.

Opens a brand-new interactive SSH login to each host on its own schedule,
apart from the persistent session, and times how long it takes to become
usable. The outcome is recorded like a check named ssh_login.
"""

import asyncio
import logging
import math
import os
import random
import time

from collector import ssh_args
from ssh_checks import Check

LOGIN_PROBE_INTERVAL_S = 300
LOGIN_PROBE_OFFSET_S = 150         # halfway between check cycles
LOGIN_PROBE_TIMEOUT_S = 60
LOGIN_PROBE_BACKOFF_AFTER = 2      # consecutive timeouts before backing off
LOGIN_PROBE_BACKOFF_INTERVAL_S = 900
LOGIN_CHECK = Check("ssh_login", "", LOGIN_PROBE_INTERVAL_S, LOGIN_PROBE_TIMEOUT_S)

log = logging.getLogger("collector")


async def probe_login(host):
    """Time one fresh, interactive SSH login until the shell accepts input.

    Returns (status, stages) where stages maps stage name -> seconds for every
    stage that completed:
      connect  DNS lookup and TCP connect
      auth     key exchange and authentication (on our targets this reads
               ~/.ssh/authorized_keys from the NFS home directory)
      shell    session setup, PTY, login shell and profile, until a typed
               command runs
      total    all of the above
    A stuck login shows as the stages up to the one it hung in.
    """
    a, b = random.randint(1000, 9999), random.randint(1000, 9999)
    # The PTY echoes what we type straight back; the echo contains "$((a+b))",
    # only the shell's output contains the sum. The shell reads this typed-ahead
    # line once it is ready for input, i.e. "at the prompt".
    typed = f"echo GADGET_$(({a}+{b}))_READY; exit\n".encode()
    marker = f"GADGET_{a + b}_READY".encode()
    cmd = ssh_args(host, "-tt", "-v", "-o", "ControlMaster=no", "-o", "ControlPath=none")

    t0 = time.monotonic()
    marks = {}
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, env={**os.environ, "TERM": "xterm"},
    )
    proc.stdin.write(typed)

    async def watch_stderr():
        while line := await proc.stderr.readline():
            if b"Connection established" in line:
                marks.setdefault("connected", time.monotonic())
            elif b"Authenticated to" in line:
                marks.setdefault("authenticated", time.monotonic())

    async def watch_stdout():
        seen = b""
        while chunk := await proc.stdout.read(65536):
            seen = (seen + chunk)[-4096:]
            if marker in seen:
                marks["ready"] = time.monotonic()
                return True
        return False

    err_task = asyncio.create_task(watch_stderr())
    status = "error"
    try:
        if await asyncio.wait_for(watch_stdout(), LOGIN_PROBE_TIMEOUT_S):
            status = "ok"
    except asyncio.TimeoutError:
        status = "timeout"
    finally:
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        err_task.cancel()

    stages, prev = {}, t0
    for stage, mark in (("connect", "connected"), ("auth", "authenticated"), ("shell", "ready")):
        if mark not in marks:
            break
        stages[stage] = marks[mark] - prev
        prev = marks[mark]
    if status == "ok":
        stages["total"] = marks["ready"] - t0
    return status, stages


async def login_probe_loop(host):
    """Probe host's login every 5 minutes; every 15 while logins time out.

    Probes run one at a time per host. A hung login can leave stuck
    processes on the target that we cannot clean up, so after repeated
    timeouts we probe less often until one succeeds.
    """
    offset = host.offset + LOGIN_PROBE_OFFSET_S
    timeouts = 0
    while True:
        interval = (LOGIN_PROBE_BACKOFF_INTERVAL_S if timeouts >= LOGIN_PROBE_BACKOFF_AFTER
                    else LOGIN_PROBE_INTERVAL_S)
        now = time.time()
        await asyncio.sleep(math.floor((now - offset) / interval + 1) * interval + offset - now)
        ts_ms = int(time.time() * 1000)
        try:
            status, stages = await probe_login(host)
        except OSError as e:
            log.warning("%s: login probe could not start ssh: %s", host.name, e)
            status, stages = "error", {}
        timeouts = timeouts + 1 if status == "timeout" else 0
        for stage, secs in stages.items():
            host.writer.metric("gadget_ssh_login_seconds", host.labels(stage=stage), round(secs, 3), ts_ms)
        host.record(LOGIN_CHECK, status, stages.get("total"))
        if status != "ok":
            log.warning("%s: login probe %s after stages %s", host.name, status,
                        {k: round(v, 2) for k, v in stages.items()})
