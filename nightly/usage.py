#!/usr/bin/env python3
"""Snapshot the Claude plan usage around nightly runs, and report on it.

    usage.py snap RUN PHASE   append one snapshot to the usage file, print it
    usage.py report [N]       per-run usage for the last N runs (default 14)

run.sh takes a "start" snapshot before the session, a "sample" every few
minutes while it runs, and an "end" one after it stops. The numbers are the
ones /usage shows in Claude Code: the percentage used of the 5-hour window and
of the weekly limit, for the whole account (anyone else's use of it counts
too). The question the report answers is how much of a 5-hour window a
typical night takes, so a later version of run.sh can decide whether there is
room to start another session.

The endpoint is the one Claude Code itself calls for /usage. It isn't a
documented API, so everything here fails soft: a failed snapshot is recorded
as an error and never stops the run.
"""
import json
import os
import statistics
import sys
import time
import urllib.request
from datetime import datetime, timezone

USAGE_FILE = os.environ.get("NIGHTLY_USAGE_FILE", "/root/gadget-nightly/usage.jsonl")
CREDENTIALS = "/root/.claude/.credentials.json"
URL = "https://api.anthropic.com/api/oauth/usage"


def fetch():
    with open(CREDENTIALS) as f:
        oauth = json.load(f)["claudeAiOauth"]
    # Claude Code refreshes the token when it next runs. Until then (e.g. at
    # 1 AM after a quiet evening) there is nothing to do but wait for it.
    if oauth.get("expiresAt", 0) / 1000 < time.time():
        raise RuntimeError("OAuth token expired; Claude Code renews it on its next request")
    req = urllib.request.Request(URL, headers={
        "Authorization": "Bearer " + oauth["accessToken"],
        "anthropic-beta": "oauth-2025-04-20",
        "User-Agent": "gadget-monitor-nightly",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        u = json.load(r)
    out = {}
    for key in ("five_hour", "seven_day"):
        w = u.get(key) or {}
        out[key] = w.get("utilization")
        out[key + "_resets_at"] = w.get("resets_at")
    return out


def snap(run, phase):
    rec = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "run": run, "phase": phase}
    try:
        rec.update(fetch())
    except Exception as e:  # noqa: BLE001 -- anything here just means "no number"
        rec["error"] = f"{type(e).__name__}: {e}"
    with open(USAGE_FILE, "a") as f:
        f.write(json.dumps(rec) + "\n")
    if "error" in rec:
        print(f"usage {phase}: unavailable ({rec['error']})")
    else:
        print(f"usage {phase}: 5-hour window {rec['five_hour']:g}% "
              f"(resets {rec['five_hour_resets_at']}), week {rec['seven_day']:g}%")


def window_used(snaps):
    """Percentage points of 5-hour window used across a run's snapshots.

    A run can span a window reset (if something else opened the window
    before 1 AM), so add up the use within each window, keyed by its reset
    time. The window open at the first snapshot counts from that snapshot;
    any window opened later (or one with no reset time: not open yet, 0%)
    counts from 0.
    """
    def window(s):
        # The reset time varies by a fraction of a second from call to call.
        t = s["five_hour_resets_at"]
        if t is None:
            return None
        return round(datetime.fromisoformat(t).timestamp() / 60)

    by_window = {}
    for s in snaps:
        by_window.setdefault(window(s), []).append(s["five_hour"])
    first = window(snaps[0])
    return sum(max(vals) - (vals[0] if key == first else 0)
               for key, vals in by_window.items() if key is not None)


def report(n):
    runs = {}
    try:
        with open(USAGE_FILE) as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                runs.setdefault(rec["run"], []).append(rec)
    except FileNotFoundError:
        print(f"no usage recorded yet ({USAGE_FILE})")
        return

    rows = []
    for run, recs in list(runs.items())[-n:]:
        ok = [r for r in recs if "error" not in r and r.get("five_hour") is not None]
        if not ok:
            rows.append((run, None))
            continue
        t0 = datetime.fromisoformat(recs[0]["at"])
        t1 = datetime.fromisoformat(recs[-1]["at"])
        rows.append((run, {
            "start": ok[0]["five_hour"], "end": ok[-1]["five_hour"],
            "peak": max(r["five_hour"] for r in ok),
            "used": window_used(ok),
            "week": ok[-1]["seven_day"] - ok[0]["seven_day"],
            "minutes": (t1 - t0).total_seconds() / 60,
            "missed_start": ok[0] is not recs[0],
        }))

    print(f"{'run':<22} {'start%':>6} {'end%':>5} {'peak%':>5} {'used':>5} {'week':>5} {'min':>4}")
    for run, r in rows:
        if r is None:
            print(f"{run:<22} (no successful snapshots)")
            continue
        print(f"{run:<22} {r['start']:>6g} {r['end']:>5g} {r['peak']:>5g} "
              f"{r['used']:>5g} {r['week']:>5g} {r['minutes']:>4.0f}"
              + ("  (no start snapshot)" if r["missed_start"] else ""))
    used = [r["used"] for _, r in rows if r]
    if used:
        print(f"5-hour window used per run, last {len(used)}: "
              f"median {statistics.median(used):g}, max {max(used):g} percentage points")


def main():
    if len(sys.argv) == 4 and sys.argv[1] == "snap":
        snap(sys.argv[2], sys.argv[3])
    elif len(sys.argv) in (2, 3) and sys.argv[1] == "report":
        report(int(sys.argv[2]) if len(sys.argv) == 3 else 14)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
