"""gadget-monitor remote agent.

Runs on a monitored host for the lifetime of one SSH session. The collector
starts it with a tiny bootstrap (see collector.py: REMOTE_CMD) that reads this
file's source from stdin, so nothing is installed on the target.

Protocol: newline-delimited JSON on stdin/stdout.
  -> {"type": "run", "id": 17, "check": "memory", "script": "...", "timeout": 30}
  <- {"id": 17, "check": "memory", "status": "ok|error|timeout|skipped_running",
      "ts_ms": ..., "duration_s": ..., "rc": 0, "stdout": "...", "stderr": "..."}
  -> {"type": "ping", "id": 18}
  <- {"id": 18, "type": "pong", "ts_ms": ...}

If nothing arrives from the collector for IDLE_EXIT_S (it pings every 60 s),
the agent assumes the session is dead and exits. sshd on the target may never
notice a client that vanished during a network outage, and without this the
orphaned agent would linger.

A check whose previous run is still alive (for example stuck in
uninterruptible sleep on a hung NFS mount, where even SIGKILL cannot reap it)
is not started again; the agent answers "skipped_running" instead, so stuck
processes never pile up on a failing host.

Must stay compatible with the targets' system python3 (3.9, stdlib only).
"""

import json
import os
import signal
import subprocess
import sys
import threading
import time

VERSION = 1
KILL_GRACE_S = 5
IDLE_EXIT_S = 180
MAX_OUTPUT_BYTES = 1 << 20
CHECK_ENV = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C",
    "LC_ALL": "C",
}

_out_lock = threading.Lock()
_running_lock = threading.Lock()
_running = {}  # check name -> Popen
_last_rx = time.monotonic()


def now_ms():
    return int(time.time() * 1000)


def send(msg):
    data = (json.dumps(msg, separators=(",", ":")) + "\n").encode()
    with _out_lock:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


def _killpg(proc, sig):
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def _reap_later(name, proc):
    """Keep `name` marked as running until its process finally exits."""
    def wait():
        proc.wait()
        with _running_lock:
            if _running.get(name) is proc:
                del _running[name]
    threading.Thread(target=wait, daemon=True).start()


def run_check(req):
    rid, name = req["id"], req["check"]
    timeout = float(req.get("timeout", 30))
    with _running_lock:
        if name in _running:
            send({"id": rid, "check": name, "status": "skipped_running", "ts_ms": now_ms()})
            return
        ts = now_ms()
        start = time.monotonic()
        try:
            proc = subprocess.Popen(
                ["/bin/bash", "-s"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd="/", env=CHECK_ENV, start_new_session=True, close_fds=True,
            )
        except OSError as e:
            send({"id": rid, "check": name, "status": "error", "ts_ms": ts,
                  "duration_s": 0, "rc": None, "stdout": "", "stderr": "spawn failed: %s" % e})
            return
        _running[name] = proc

    status = None
    try:
        out, err = proc.communicate(req["script"].encode(), timeout=timeout)
    except subprocess.TimeoutExpired:
        status = "timeout"
        _killpg(proc, signal.SIGTERM)
        try:
            out, err = proc.communicate(timeout=KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            _killpg(proc, signal.SIGKILL)
            try:
                out, err = proc.communicate(timeout=KILL_GRACE_S)
            except subprocess.TimeoutExpired:
                out, err = b"", b"process did not exit after SIGKILL"
    duration = time.monotonic() - start

    if proc.returncode is None:
        _reap_later(name, proc)
    else:
        with _running_lock:
            _running.pop(name, None)

    if status is None:
        status = "ok" if proc.returncode == 0 else "error"
    send({
        "id": rid, "check": name, "status": status, "ts_ms": ts,
        "duration_s": round(duration, 3), "rc": proc.returncode,
        "stdout": out[:MAX_OUTPUT_BYTES].decode(errors="replace"),
        "stderr": err[:4096].decode(errors="replace"),
    })


def shutdown():
    """stdin closed: the session is gone. Kill what we started and leave."""
    with _running_lock:
        for proc in _running.values():
            _killpg(proc, signal.SIGKILL)
    os._exit(0)


def idle_watchdog():
    while True:
        time.sleep(10)
        if time.monotonic() - _last_rx > IDLE_EXIT_S:
            shutdown()


def main():
    global _last_rx
    threading.Thread(target=idle_watchdog, daemon=True).start()
    send({"type": "hello", "version": VERSION, "ts_ms": now_ms(),
          "pid": os.getpid(), "hostname": os.uname().nodename})
    for line in sys.stdin.buffer:
        _last_rx = time.monotonic()
        try:
            req = json.loads(line)
        except ValueError:
            continue
        kind = req.get("type")
        if kind == "ping":
            send({"id": req.get("id"), "type": "pong", "ts_ms": now_ms()})
        elif kind == "run":
            threading.Thread(target=run_check, args=(req,), daemon=True).start()
    shutdown()


main()
