#!/usr/bin/env python3
"""Snapshot the Claude plan usage around nightly runs, and report on it.

    usage.py snap RUN PHASE   append one snapshot to the usage file, print it
    usage.py report [N]       usage for each of the last N sessions (default 14)
    usage.py room             exit 0 if there is room for one more session

run.sh takes a "start" snapshot before each session, a "sample" every few
minutes while it runs, and an "end" one after it stops. The numbers are the
ones /usage shows in Claude Code: the percentage used of the 5-hour window and
of the weekly limit, for the whole account (anyone else's use of it counts
too). The report says how much of a 5-hour window a typical session takes;
"room" uses that to decide whether run.sh may start another session tonight.

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

def conf_work():
    """WORK from nightly.conf next to this script, so a hand-run report finds
    the same file run.sh writes (run.sh also passes it as NIGHTLY_USAGE_FILE)."""
    conf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nightly.conf")
    with open(conf) as f:
        for line in f:
            if line.startswith("WORK="):
                return line.split("=", 1)[1].split("#")[0].strip().strip("\"'")
    raise SystemExit(f"no WORK= line in {conf}")


USAGE_FILE = os.environ.get("NIGHTLY_USAGE_FILE") or os.path.join(conf_work(), "usage.jsonl")
CREDENTIALS = os.path.join(
    os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude"), ".credentials.json")
URL = "https://api.anthropic.com/api/oauth/usage"
# "room" leaves this many percentage points of the 5-hour window, and of the
# week, unused on top of the most any recent session has taken. The week's
# buffer is large because running out of it locks the maintainer out for days.
FIVE_HOUR_BUFFER = float(os.environ.get("NIGHTLY_FIVE_HOUR_BUFFER", 15))
WEEK_BUFFER = float(os.environ.get("NIGHTLY_WEEK_BUFFER", 30))
# Sessions to size the next one from: the recent ones, so the estimate
# follows changes in the prompt or the issues.
ROOM_SESSIONS = 14


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


def load_sessions():
    """The snapshots grouped by session, oldest first.

    A session starts at each "start" snapshot, so a run started twice under
    the same name (by hand, or before sessions were numbered) still counts as
    two sessions.
    """
    sessions = []
    with open(USAGE_FILE) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if (rec.get("phase") == "start" or not sessions
                    or sessions[-1][0] != rec["run"]):
                sessions.append((rec["run"], []))
            sessions[-1][1].append(rec)
    return sessions


def summarize(recs):
    """A session's usage, or None if no snapshot of it succeeded."""
    ok = [r for r in recs if "error" not in r and r.get("five_hour") is not None]
    if not ok:
        return None
    t0 = datetime.fromisoformat(recs[0]["at"])
    t1 = datetime.fromisoformat(recs[-1]["at"])
    return {
        "start": ok[0]["five_hour"], "end": ok[-1]["five_hour"],
        "peak": max(r["five_hour"] for r in ok),
        "used": window_used(ok),
        "week": ok[-1]["seven_day"] - ok[0]["seven_day"],
        "minutes": (t1 - t0).total_seconds() / 60,
        "missed_start": ok[0] is not recs[0],
    }


def report(n):
    try:
        sessions = load_sessions()
    except FileNotFoundError:
        print(f"no usage recorded yet ({USAGE_FILE})")
        return
    rows = [(run, summarize(recs)) for run, recs in sessions[-n:]]

    print(f"{'session':<22} {'start%':>6} {'end%':>5} {'peak%':>5} {'used':>5} {'week':>5} {'min':>4}")
    for run, r in rows:
        if r is None:
            print(f"{run:<22} (no successful snapshots)")
            continue
        print(f"{run:<22} {r['start']:>6g} {r['end']:>5g} {r['peak']:>5g} "
              f"{r['used']:>5g} {r['week']:>5g} {r['minutes']:>4.0f}"
              + ("  (no start snapshot)" if r["missed_start"] else ""))
    used = [r["used"] for _, r in rows if r]
    if used:
        print(f"5-hour window used per session, last {len(used)}: "
              f"median {statistics.median(used):g}, max {max(used):g} percentage points")


def room():
    """Whether another session fits in what is left of the plan.

    Reads the newest snapshot (run.sh takes an "end" one just before asking)
    and needs room for the most any recent session took, plus the buffers.
    With no usable numbers the answer is no: better a session lost than the
    maintainer locked out of the plan.
    """
    try:
        sessions = load_sessions()
    except FileNotFoundError:
        return False, f"no usage recorded yet ({USAGE_FILE})"
    if not sessions:
        return False, "no usage recorded yet"
    now = sessions[-1][1][-1]
    if "error" in now or now.get("five_hour") is None:
        return False, f"no current usage ({now.get('error', 'empty snapshot')})"
    # The newest session has a good snapshot (now), so this isn't empty.
    past = [r for r in (summarize(recs) for _, recs in sessions[-ROOM_SESSIONS:]) if r]
    need, need_week = max(r["used"] for r in past), max(r["week"] for r in past)
    five, week = now["five_hour"], now["seven_day"]
    msg = (f"5-hour window {five:g}% + {need:g} (max of last {len(past)} sessions) "
           f"+ {FIVE_HOUR_BUFFER:g} buffer, week {week:g}% + {need_week:g} + {WEEK_BUFFER:g} buffer")
    return (five + need + FIVE_HOUR_BUFFER <= 100
            and week + need_week + WEEK_BUFFER <= 100), msg


def main():
    if len(sys.argv) == 4 and sys.argv[1] == "snap":
        snap(sys.argv[2], sys.argv[3])
    elif len(sys.argv) in (2, 3) and sys.argv[1] == "report":
        report(int(sys.argv[2]) if len(sys.argv) == 3 else 14)
    elif sys.argv[1:] == ["room"]:
        ok, msg = room()
        print(("room for another session: " if ok else "no room for another session: ") + msg)
        sys.exit(0 if ok else 1)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
