"""Website probe: fetches each URL in config/websites.toml from this VM."""

import asyncio
import http.client
import logging
import math
import re
import socket
import ssl
import time
import tomllib
import urllib.parse

from collector import SRV

WEBSITES_FILE = SRV / "config" / "websites.toml"
WEBSITE_STATUSES = ("ok", "http_error", "timeout", "dns_error", "connect_error", "tls_error", "error")
WEBSITE_OFFSET_S = 30              # between the hosts' check cycles
WEBSITE_MAX_REDIRECTS = 5
WEBSITE_MAX_BODY_BYTES = 10 << 20  # read at most this much of a page, then stop

log = logging.getLogger("collector")


class WebsiteFailed(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = status


def fetch_website(url, timeout, keep_body=False):
    """GET url as a browser would, following redirects, and time each stage.

    Blocking; run it in a thread. Returns (status, http_code, stages,
    cert_expiry, detail), plus the final page's body if keep_body. stages maps stage name -> seconds, summed over
    redirects, for every stage that completed:
      dns       name lookup
      connect   TCP connect
      tls       TLS handshake and certificate check
      response  request sent until the status line and headers arrive
      body      reading the page
      total     everything, including a failed attempt up to the failure
    """
    t0 = time.monotonic()
    deadline = t0 + timeout
    stages, code, cert_expiry, body = {}, 0, None, []
    ctx = ssl.create_default_context()

    def remaining():
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError
        return left

    def timed(stage, fail_status, fn):
        t = time.monotonic()
        try:
            result = fn()
        except TimeoutError:
            raise WebsiteFailed("timeout", f"{stage} timed out")
        except (OSError, http.client.HTTPException) as e:
            raise WebsiteFailed(fail_status, f"{stage}: {e!r}")
        stages[stage] = stages.get(stage, 0.0) + time.monotonic() - t
        return result

    def read_body(resp):
        body.clear()
        # http.client has already let go of the socket (Connection: close), so
        # each read keeps the timeout set before the request; check the
        # deadline between reads instead.
        n = 0
        while n < WEBSITE_MAX_BODY_BYTES:
            remaining()
            chunk = resp.read(65536)
            if not chunk:
                break
            n += len(chunk)
            if keep_body:
                body.append(chunk)

    status, detail = "http_error", ""
    try:
        for _ in range(WEBSITE_MAX_REDIRECTS + 1):
            u = urllib.parse.urlsplit(url)
            https = u.scheme == "https"
            port = u.port or (443 if https else 80)
            family, type_, proto, _, addr = timed("dns", "dns_error", lambda: socket.getaddrinfo(
                u.hostname, port, type=socket.SOCK_STREAM)[0])
            sock = socket.socket(family, type_, proto)
            try:
                timed("connect", "connect_error", lambda: (sock.settimeout(remaining()), sock.connect(addr)))
                if https:
                    sock = timed("tls", "tls_error", lambda: (
                        sock.settimeout(remaining()), ctx.wrap_socket(sock, server_hostname=u.hostname))[1])
                    cert_expiry = ssl.cert_time_to_seconds(sock.getpeercert()["notAfter"])
                conn = http.client.HTTPConnection(u.hostname, port)
                conn.sock = sock
                path = (u.path or "/") + (f"?{u.query}" if u.query else "")

                def request():
                    sock.settimeout(remaining())
                    conn.request("GET", path, headers={
                        "Host": u.netloc, "User-Agent": "gadget-monitor", "Connection": "close"})
                    return conn.getresponse()
                resp = timed("response", "error", request)
                code = resp.status
                timed("body", "error", lambda: read_body(resp))
            finally:
                sock.close()
            location = resp.getheader("Location")
            if code in (301, 302, 303, 307, 308) and location:
                url = urllib.parse.urljoin(url, location)
                continue
            status = "ok" if code == 200 else "http_error"
            detail = f"HTTP {code}"
            break
        else:
            detail = f"more than {WEBSITE_MAX_REDIRECTS} redirects"
    except WebsiteFailed as e:
        status, detail = e.status, str(e)
    stages["total"] = time.monotonic() - t0
    if keep_body:
        return status, code, stages, cert_expiry, detail, b"".join(body)
    return status, code, stages, cert_expiry, detail


class Website:
    def __init__(self, url, interval, timeout, offset, writer):
        self.url = url
        self.site = re.sub(r"^https?://", "", url).rstrip("/")
        self.interval = interval
        self.timeout = timeout
        self.offset = offset
        self.writer = writer

    async def probe_loop(self):
        """Fetch the site every interval, one fetch at a time.

        The fetch has its own deadline, but a DNS lookup can't be interrupted;
        if one is still stuck from a previous probe, record a timeout rather
        than start another.
        """
        pending = None
        while True:
            now = time.time()
            await asyncio.sleep(math.floor((now - self.offset) / self.interval + 1) * self.interval
                                + self.offset - now)
            ts_ms = int(time.time() * 1000)
            if pending is None or pending.done():
                pending = asyncio.ensure_future(asyncio.to_thread(fetch_website, self.url, self.timeout))
            try:
                status, code, stages, cert_expiry, detail = await asyncio.wait_for(
                    asyncio.shield(pending), self.timeout + 5)
            except asyncio.TimeoutError:
                status, code, stages, cert_expiry, detail = "timeout", 0, {}, None, "probe stuck"
            except Exception as e:
                status, code, stages, cert_expiry, detail = "error", 0, {}, None, repr(e)
            w, lb = self.writer, {"site": self.site}
            w.metric("gadget_website_up", lb, 1 if status == "ok" else 0, ts_ms)
            for s in WEBSITE_STATUSES:
                w.metric("gadget_website_status", {**lb, "status": s}, 1 if s == status else 0, ts_ms)
            w.metric("gadget_website_http_status", lb, code, ts_ms)
            for stage, secs in stages.items():
                w.metric("gadget_website_seconds", {**lb, "stage": stage}, round(secs, 4), ts_ms)
            if cert_expiry is not None:
                w.metric("gadget_website_cert_expiry_timestamp_seconds", lb, cert_expiry, ts_ms)
            if status != "ok":
                log.warning("website %s: %s (%s)", self.site, status, detail)


def load_websites(writer):
    cfg = tomllib.loads(WEBSITES_FILE.read_text())
    interval, timeout = cfg.get("interval", 60), cfg.get("timeout", 15)
    return [Website(url, interval, timeout, WEBSITE_OFFSET_S + 2 * i, writer)
            for i, url in enumerate(cfg.get("urls", []))]

