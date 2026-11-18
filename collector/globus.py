"""Globus Connect Server probe: each node in config/globus.toml, from this VM.

A GCS v5 node runs Apache on 443 in front of the GCS Manager API. The
Manager's GET /api/info needs no credentials and answers {"code": "success",
...} only when the Manager behind Apache is working, so that is the health
bit. Apache serves it only on the endpoint's Globus domain
(*.data.globus.org or *.gaccess.io), which resolves to every node of the
endpoint; to test one node, the probe connects to that node's address and
sends the domain as TLS SNI and Host, verifying the certificate against it.

The probe ends in one of four states:
  healthy      /api/info answered 200 with code "success"
  unhealthy    the node's HTTPS port answered, but not that (a 502 from
               Apache with the Manager down, a certificate that doesn't
               verify, a broken answer)
  unreachable  no answer on the HTTPS port at all (refused, timed out, no
               route, DNS failure)
  unknown      no domain configured for the node: the port answered, but
               without the domain there is nothing to ask
"""

import asyncio
import http.client
import json
import logging
import math
import socket
import ssl
import time
import tomllib

from collector import SRV

GLOBUS_FILE = SRV / "config" / "globus.toml"
GLOBUS_OFFSET_S = 40               # clear of the hosts', websites', storage and postgres probes
GLOBUS_STATES = ("healthy", "unhealthy", "unreachable", "unknown")
MAX_BODY_BYTES = 1 << 20           # /api/info is a few KB

log = logging.getLogger("collector")


class ProbeFailed(Exception):
    def __init__(self, state, detail):
        super().__init__(detail)
        self.state = state


def probe_node(address, domain, port, timeout):
    """Ask one node's GCS Manager for /api/info. Blocking; run it in a thread.

    Returns (state, http_code, stages, cert_expiry, version, detail). stages
    maps connect / tls / response / total -> seconds, for the stages that
    completed; total is also set when the probe fails.
    """
    t0 = time.monotonic()
    deadline = t0 + timeout
    stages, code, cert_expiry, version = {}, 0, None, None

    def remaining():
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError
        return left

    def timed(stage, fail_state, fn):
        t = time.monotonic()
        try:
            result = fn()
        except TimeoutError:
            # Past connect, the port did answer: a hang there is the server's fault.
            raise ProbeFailed("unreachable" if stage == "connect" else "unhealthy", f"{stage} timed out")
        except (OSError, http.client.HTTPException, ValueError) as e:
            raise ProbeFailed(fail_state, f"{stage}: {e!r}")
        stages[stage] = time.monotonic() - t
        return result

    if domain:
        ctx = ssl.create_default_context()
    else:
        # The default vhost serves Apache's self-signed certificate; nothing to verify.
        ctx = ssl.create_default_context()
        ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE

    sock = None
    try:
        sock = timed("connect", "unreachable", lambda: socket.create_connection((address, port), remaining()))
        sock = timed("tls", "unhealthy", lambda: (
            sock.settimeout(remaining()), ctx.wrap_socket(sock, server_hostname=domain or address))[1])
        if domain:
            cert_expiry = ssl.cert_time_to_seconds(sock.getpeercert()["notAfter"])
        conn = http.client.HTTPConnection(domain or address, port)
        conn.sock = sock

        def request():
            sock.settimeout(remaining())
            conn.request("GET", "/api/info", headers={
                "Host": domain or address, "User-Agent": "gadget-monitor", "Connection": "close"})
            resp = conn.getresponse()
            return resp.status, resp.read(MAX_BODY_BYTES)
        code, body = timed("response", "unhealthy", request)
        if not domain:
            state, detail = "unknown", f"HTTP {code}; no domain configured"
        elif code != 200:
            state, detail = "unhealthy", f"HTTP {code}"
        else:
            try:
                info = json.loads(body)
                ok = info.get("code") == "success"
                version = next((d.get("manager_version") for d in info.get("data", [])
                                if str(d.get("DATA_TYPE", "")).startswith("info#")), None)
            except (ValueError, AttributeError):
                ok = False
            state, detail = ("healthy", "") if ok else ("unhealthy", "/api/info: no success code")
    except ProbeFailed as e:
        state, detail = e.state, str(e)
    except socket.gaierror as e:
        state, detail = "unreachable", f"dns: {e!r}"
    finally:
        if sock is not None:
            sock.close()
    stages["total"] = time.monotonic() - t0
    return state, code, stages, cert_expiry, version, detail


class GlobusNode:
    def __init__(self, cfg, interval, timeout, offset, writer):
        self.name = cfg["name"]
        self.address = cfg["address"]
        self.domain = cfg.get("domain", "")
        self.port = cfg.get("port", 443)
        self.interval = interval
        self.timeout = timeout
        self.offset = offset
        self.writer = writer

    async def probe_loop(self):
        """Probe every interval, one probe at a time.

        The probe has its own deadline, but a DNS lookup can't be interrupted;
        if one is still stuck from a previous probe, record it as unreachable
        rather than start another.
        """
        pending = None
        while True:
            now = time.time()
            await asyncio.sleep(math.floor((now - self.offset) / self.interval + 1) * self.interval
                                + self.offset - now)
            ts_ms = int(time.time() * 1000)
            if pending is None or pending.done():
                pending = asyncio.ensure_future(asyncio.to_thread(
                    probe_node, self.address, self.domain, self.port, self.timeout))
            try:
                state, code, stages, cert_expiry, version, detail = await asyncio.wait_for(
                    asyncio.shield(pending), self.timeout + 5)
            except asyncio.TimeoutError:
                state, code, stages, cert_expiry, version, detail = "unreachable", 0, {}, None, None, "probe stuck"
            except Exception as e:
                state, code, stages, cert_expiry, version, detail = "unhealthy", 0, {}, None, None, repr(e)
            w, lb = self.writer, {"server": self.name}
            w.metric("gadget_globus_up", lb, 1 if state == "healthy" else 0, ts_ms)
            for s in GLOBUS_STATES:
                w.metric("gadget_globus_state", {**lb, "state": s}, 1 if s == state else 0, ts_ms)
            w.metric("gadget_globus_http_status", lb, code, ts_ms)
            for stage, secs in stages.items():
                w.metric("gadget_globus_seconds", {**lb, "stage": stage}, round(secs, 4), ts_ms)
            if cert_expiry is not None:
                w.metric("gadget_globus_cert_expiry_timestamp_seconds", lb, cert_expiry, ts_ms)
            if version:
                # A new series only when GCS is upgraded: a handful a year.
                w.metric("gadget_globus_manager_info", {**lb, "version": version}, 1, ts_ms)
            if state != "healthy":
                log.warning("globus %s: %s (%s)", self.name, state, detail)


def load_globus(writer):
    if not GLOBUS_FILE.exists():
        return []
    cfg = tomllib.loads(GLOBUS_FILE.read_text())
    interval, timeout = cfg.get("interval", 60), cfg.get("timeout", 15)
    return [GlobusNode(s, interval, timeout, GLOBUS_OFFSET_S + 2 * i, writer)
            for i, s in enumerate(cfg.get("server", []))]
