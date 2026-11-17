"""JupyterHub probe: asks each hub in config/jupyterhubs.toml for its API root.

GET <url>hub/api needs no token and answers {"version": "..."} from the hub
process itself, so a 200 with a version means the whole path a user takes
(the host's Apache, configurable-http-proxy, the hub) is up. It does not say
whether a server or kernel can start; that needs the hub's API token (#1).
"""

import asyncio
import json
import logging
import math
import time
import tomllib

from collector import SRV
from websites import WEBSITE_STATUSES, fetch_website

JUPYTERHUBS_FILE = SRV / "config" / "jupyterhubs.toml"
JUPYTERHUB_OFFSET_S = 35           # after the websites' probes, clear of the hosts' and storage

log = logging.getLogger("collector")


def probe_hub(url, timeout):
    """Fetch the hub's API root. Blocking; run it in a thread.

    Returns fetch_website's (status, http_code, stages, cert_expiry, detail)
    and the hub's version. A 200 that isn't the hub's answer (a proxy or
    login page in the way) counts as an error.
    """
    status, code, stages, cert_expiry, detail, body = fetch_website(url + "hub/api", timeout, keep_body=True)
    version = None
    if status == "ok":
        try:
            version = json.loads(body)["version"]
        except (ValueError, KeyError, TypeError):
            pass
        if not isinstance(version, str):
            status, detail, version = "error", "hub/api: no version in the answer", None
    return status, code, stages, cert_expiry, version, detail


class Hub:
    def __init__(self, cfg, interval, timeout, offset, writer):
        self.name = cfg["name"]
        self.url = cfg["url"].rstrip("/") + "/"
        self.interval = interval
        self.timeout = timeout
        self.offset = offset
        self.writer = writer

    async def probe_loop(self):
        """Probe every interval, one probe at a time (see Website.probe_loop)."""
        pending = None
        while True:
            now = time.time()
            await asyncio.sleep(math.floor((now - self.offset) / self.interval + 1) * self.interval
                                + self.offset - now)
            ts_ms = int(time.time() * 1000)
            if pending is None or pending.done():
                pending = asyncio.ensure_future(asyncio.to_thread(probe_hub, self.url, self.timeout))
            try:
                status, code, stages, cert_expiry, version, detail = await asyncio.wait_for(
                    asyncio.shield(pending), self.timeout + 5)
            except asyncio.TimeoutError:
                status, code, stages, cert_expiry, version, detail = "timeout", 0, {}, None, None, "probe stuck"
            except Exception as e:
                status, code, stages, cert_expiry, version, detail = "error", 0, {}, None, None, repr(e)
            w, lb = self.writer, {"hub": self.name}
            w.metric("gadget_jupyterhub_up", lb, 1 if status == "ok" else 0, ts_ms)
            for s in WEBSITE_STATUSES:
                w.metric("gadget_jupyterhub_status", {**lb, "status": s}, 1 if s == status else 0, ts_ms)
            w.metric("gadget_jupyterhub_http_status", lb, code, ts_ms)
            for stage, secs in stages.items():
                w.metric("gadget_jupyterhub_seconds", {**lb, "stage": stage}, round(secs, 4), ts_ms)
            if cert_expiry is not None:
                w.metric("gadget_jupyterhub_cert_expiry_timestamp_seconds", lb, cert_expiry, ts_ms)
            if version:
                # A new series only when the hub is upgraded: a few a year.
                w.metric("gadget_jupyterhub_info", {**lb, "version": version}, 1, ts_ms)
            if status != "ok":
                log.warning("jupyterhub %s: %s (%s)", self.name, status, detail)


def load_jupyterhubs(writer):
    if not JUPYTERHUBS_FILE.exists():
        return []
    cfg = tomllib.loads(JUPYTERHUBS_FILE.read_text())
    interval, timeout = cfg.get("interval", 60), cfg.get("timeout", 15)
    return [Hub(h, interval, timeout, JUPYTERHUB_OFFSET_S + 2 * i, writer)
            for i, h in enumerate(cfg.get("hub", []))]
