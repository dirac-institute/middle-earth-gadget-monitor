"""xrootd probe: each server in config/xrootd.toml, from this VM.

Outside-in only, for now: the servers need GSI credentials before they answer
anything useful (stats, errors, transfers), and the collector has none. So
each probe checks, in parallel:

  vm       TCP connect to vm_port (22): any answer, even a refusal, means
           the host is up
  xrootd   speak the xrootd protocol as a client would, as far as login:
           connect, handshake, switch to TLS if the server asks, log in.
           A login answered with "authenticate first" counts as up: the
           server is accepting and processing sessions. The probe never
           authenticates, so it opens no files and moves no data.

Stages are timed, and the certificate the server presents for TLS (its host
certificate, also used for GSI) is read for its expiry date.
"""

import asyncio
import datetime
import logging
import math
import os
import socket
import ssl
import struct
import time
import tomllib

from collector import SRV
from postgres import tcp_probe

XROOTD_FILE = SRV / "config" / "xrootd.toml"
XROOTD_OFFSET_S = 36               # clear of the websites' (30-) and storage (45) probes
XROOTD_STATUSES = ("ok", "refused", "timeout", "connect_error", "tls_error", "protocol_error", "error")

# From XProtocol.hh.
KXR_PROTOCOL, KXR_LOGIN = 3006, 3007
KXR_OK, KXR_AUTHMORE = 0, 4002
KXR_SECREQS, KXR_ABLETLS, KXR_EXPLOGIN = 0x01, 0x02, 0x03
KXR_GOTOTLS = 0x40000000
CLIENT_PV = 0x00000520             # protocol version 5.2.0, what xrootd 5 clients send

log = logging.getLogger("collector")


class XrootdFailed(Exception):
    def __init__(self, status, detail):
        super().__init__(detail)
        self.status = status


def der_not_after(der):
    """notAfter of a DER X.509 certificate, as a Unix time; None if unparsable.

    Walks just far enough into the ASN.1 to reach the validity period, so the
    collector needs no X.509 library for an unverified certificate.
    """
    def tlv(buf, i):
        tag, n = buf[i], buf[i + 1]
        i += 2
        if n & 0x80:
            k = n & 0x7f
            n = int.from_bytes(buf[i:i + k], "big")
            i += k
        return tag, buf[i:i + n], i + n

    try:
        _, cert, _ = tlv(der, 0)
        _, tbs, _ = tlv(cert, 0)
        i = 0
        if tbs[0] == 0xa0:              # explicit version
            i = tlv(tbs, i)[2]
        for _ in range(3):              # serial, signature algorithm, issuer
            i = tlv(tbs, i)[2]
        _, validity, _ = tlv(tbs, i)
        _, _, j = tlv(validity, 0)      # notBefore
        tag, value, _ = tlv(validity, j)
        fmt = "%y%m%d%H%M%SZ" if tag == 0x17 else "%Y%m%d%H%M%SZ"
        return datetime.datetime.strptime(value.decode(), fmt).replace(
            tzinfo=datetime.timezone.utc).timestamp()
    except (IndexError, ValueError, UnicodeDecodeError):
        return None


def xrootd_login(address, port, timeout):
    """Connect, handshake, TLS if asked, log in. Blocking; run it in a thread.

    Returns (status, stages, cert_expiry, detail). stages maps stage name ->
    seconds for every stage that completed: connect, handshake (initial
    handshake and protocol request), tls, login, and total (also recorded, up
    to the failure, when the probe fails).
    """
    t0 = time.monotonic()
    deadline = t0 + timeout
    stages, cert_expiry = {}, None
    sock = None

    def remaining():
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError
        return left

    def timed(stage, fail_status, fn):
        t = time.monotonic()
        try:
            result = fn()
        except (TimeoutError, socket.timeout):
            raise XrootdFailed("timeout", f"{stage} timed out")
        except ConnectionRefusedError:
            raise XrootdFailed("refused", f"{stage}: connection refused")
        except ssl.SSLError as e:
            raise XrootdFailed("tls_error", f"{stage}: {e!r}")
        except OSError as e:
            raise XrootdFailed(fail_status, f"{stage}: {e!r}")
        stages[stage] = time.monotonic() - t
        return result

    def recv_exact(n):
        buf = b""
        while len(buf) < n:
            sock.settimeout(remaining())
            chunk = sock.recv(n - len(buf))
            if not chunk:
                raise XrootdFailed("protocol_error", "server closed the connection")
            buf += chunk
        return buf

    def response():
        """One server response -> (status, body)."""
        _, status, dlen = struct.unpack(">2sHi", recv_exact(8))
        if not 0 <= dlen <= 65536:
            raise XrootdFailed("protocol_error", f"response length {dlen}")
        return status, recv_exact(dlen)

    def connect():
        nonlocal sock
        sock = socket.create_connection((address, port), timeout=remaining())

    def handshake():
        # The initial handshake, and kXR_protocol in the same packet, as
        # clients do: say we can do TLS, ask what login will need.
        sock.settimeout(remaining())
        sock.sendall(struct.pack(">5i", 0, 0, 0, 4, 2012) +
                     struct.pack(">2sHiBB10si", b"\0\1", KXR_PROTOCOL, CLIENT_PV,
                                 KXR_SECREQS | KXR_ABLETLS, KXR_EXPLOGIN, b"\0" * 10, 0))
        _, status, dlen, _, server_type = struct.unpack(">2sHiii", recv_exact(16))
        if status != KXR_OK or dlen != 8 or server_type not in (0, 1):
            raise XrootdFailed("protocol_error", "not an xrootd server")
        status, body = response()
        if status != KXR_OK or len(body) < 8:
            raise XrootdFailed("protocol_error", f"kXR_protocol answered status {status}")
        return struct.unpack(">ii", body[:8])[1] & 0xffffffff

    def start_tls():
        nonlocal sock, cert_expiry
        # Grid host certificates are signed by CAs this VM doesn't carry; we
        # want the expiry date, not a verified identity.
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        sock.settimeout(remaining())
        sock = ctx.wrap_socket(sock, server_hostname=address)
        cert_expiry = der_not_after(sock.getpeercert(binary_form=True) or b"")

    def login():
        sock.settimeout(remaining())
        sock.sendall(struct.pack(">2sHi8sBBBBi", b"\0\2", KXR_LOGIN, os.getpid(),
                                 b"gadget\0\0", 0, 0, 5, 0, 0))
        status, body = response()
        # kXR_ok with a session id: logged in, possibly with "&P=..." naming
        # the authentication protocols to use next.
        if status != KXR_OK or len(body) < 16:
            raise XrootdFailed("protocol_error", f"login answered status {status}: "
                               f"{body[4:200].decode(errors='replace').strip(chr(0))}")
        return body[16:].decode(errors="replace").strip("\0")

    status, detail = "ok", ""
    try:
        timed("connect", "connect_error", connect)
        flags = timed("handshake", "protocol_error", handshake)
        if flags & KXR_GOTOTLS:
            timed("tls", "tls_error", start_tls)
        detail = timed("login", "protocol_error", login)
    except XrootdFailed as e:
        status, detail = e.status, str(e)
    except Exception as e:
        status, detail = "error", repr(e)
    finally:
        if sock is not None:
            sock.close()
    stages["total"] = time.monotonic() - t0
    return status, stages, cert_expiry, detail


class XrootdServer:
    def __init__(self, cfg, interval, timeout, offset, writer):
        self.name = cfg["name"]
        self.address = cfg["address"]
        self.port = cfg.get("port", 1094)
        self.vm_port = cfg.get("vm_port", 22)
        self.interval = interval
        self.timeout = timeout
        self.offset = offset
        self.writer = writer

    async def probe(self):
        return await asyncio.gather(
            asyncio.to_thread(tcp_probe, self.address, self.vm_port, self.timeout),
            asyncio.to_thread(xrootd_login, self.address, self.port, self.timeout))

    async def probe_loop(self):
        """Probe every interval, one probe at a time.

        Each part has its own deadline, but a DNS lookup can't be interrupted;
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
                (vm, _), (status, stages, cert_expiry, detail) = await asyncio.wait_for(
                    asyncio.shield(pending), self.timeout + 5)
            except asyncio.TimeoutError:
                vm, status, stages, cert_expiry, detail = "timeout", "timeout", {}, None, "probe stuck"
            except Exception as e:
                vm, status, stages, cert_expiry, detail = "error", "error", {}, None, repr(e)
            w, lb = self.writer, {"server": self.name}
            w.metric("gadget_xrootd_vm_up", lb, 1 if vm in ("open", "refused") else 0, ts_ms)
            w.metric("gadget_xrootd_up", lb, 1 if status == "ok" else 0, ts_ms)
            for s in XROOTD_STATUSES:
                w.metric("gadget_xrootd_status", {**lb, "status": s}, 1 if s == status else 0, ts_ms)
            for stage, secs in stages.items():
                w.metric("gadget_xrootd_seconds", {**lb, "stage": stage}, round(secs, 4), ts_ms)
            if cert_expiry is not None:
                w.metric("gadget_xrootd_cert_expiry_timestamp_seconds", lb, cert_expiry, ts_ms)
            if status != "ok":
                log.warning("xrootd %s: vm %s, xrootd %s (%s)", self.name, vm, status, detail)


def load_xrootd(writer):
    if not XROOTD_FILE.exists():
        return []
    cfg = tomllib.loads(XROOTD_FILE.read_text())
    interval, timeout = cfg.get("interval", 60), cfg.get("timeout", 15)
    return [XrootdServer(s, interval, timeout, XROOTD_OFFSET_S + 2 * i, writer)
            for i, s in enumerate(cfg.get("server", []))]
