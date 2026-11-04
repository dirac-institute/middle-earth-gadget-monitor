"""Storage probe: how big the metrics database is and how much room it has.

Every few minutes, reads VictoriaMetrics' own /metrics (data size, samples
stored and ingested, retention) and the free space on the /local partition
that holds it. The projection (when the disk fills, or how big the database
settles at) is worked out in Grafana from these; see the Monitoring storage
dashboard.
"""

import asyncio
import logging
import math
import os
import re
import time
import urllib.request
from pathlib import Path

from collector import VM_URL

LOCAL_DIR = Path("/srv/local")     # ${GADGET_LOCAL_DIR}, mounted read-only
STORAGE_INTERVAL_S = 300
STORAGE_OFFSET_S = 45              # clear of the hosts' and websites' probes

# VictoriaMetrics duration suffixes; a bare number is months.
RETENTION_UNITS = {"h": 3600, "d": 86400, "w": 7 * 86400, "y": 365 * 86400}

log = logging.getLogger("collector")


def parse_metrics(text):
    """Prometheus text -> {(name, labels text): value}, enough for VM's /metrics."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"([a-zA-Z_:][\w:]*)(\{.*\})?\s+(\S+)$", line)
        if m:
            out[(m[1], m[2] or "")] = float(m[3])
    return out


def parse_retention(value):
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([hdwy]?)", value)
    if not m:
        return None
    n = float(m[1])
    return n * RETENTION_UNITS[m[2]] if m[2] else n * 31 * 86400


def read_storage():
    """Blocking; run it in a thread. Returns {metric name: value}."""
    with urllib.request.urlopen(f"{VM_URL}/metrics", timeout=30) as resp:
        text = resp.read().decode()
    vm = parse_metrics(text)
    out = {
        # Everything on disk: samples and the series index.
        "gadget_vm_data_bytes": sum(v for (n, _), v in vm.items() if n == "vm_data_size_bytes"),
        "gadget_vm_stored_samples": sum(v for (n, lb), v in vm.items()
                                        if n == "vm_rows" and 'type="storage/' in lb),
        "gadget_vm_samples_added_total": vm.get(("vm_rows_added_to_storage_total", ""), 0),
    }
    m = re.search(r'^flag\{name="retentionPeriod", value="([^"]*)"', text, re.M)
    retention = m and parse_retention(m[1])
    if retention:
        out["gadget_vm_retention_seconds"] = retention
    st = os.statvfs(LOCAL_DIR)
    out["gadget_local_disk_size_bytes"] = st.f_blocks * st.f_frsize
    # What an unprivileged writer (VictoriaMetrics) can still use.
    out["gadget_local_disk_free_bytes"] = st.f_bavail * st.f_frsize
    return out


async def storage_loop(writer):
    while True:
        now = time.time()
        await asyncio.sleep(math.floor((now - STORAGE_OFFSET_S) / STORAGE_INTERVAL_S + 1)
                            * STORAGE_INTERVAL_S + STORAGE_OFFSET_S - now)
        ts_ms = int(time.time() * 1000)
        try:
            values = await asyncio.wait_for(asyncio.to_thread(read_storage), 60)
        except Exception as e:
            log.warning("storage probe failed: %r", e)
            continue
        writer.add([f"{name} {value:.0f} {ts_ms}" for name, value in values.items()])
