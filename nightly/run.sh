#!/bin/bash
# Nightly issue worker, run by cron as root (see nightly/crontab).
#
# Starts a background Claude Code session (named nightly-<date>) in a separate
# clone of the repo with nightly/prompt.md: it picks one actionable open issue
# (or review feedback on one of its own PRs), works on it, tests by deploying
# its branch to the live stack, and opens a PR. Once the session has sat idle
# for IDLE_STOP minutes, or the time limit is reached, it is stopped and the
# live stack is put back exactly as it was if anything about it changed.
#
# Watch it, live or afterwards, in a full Claude Code window:
#   claude attach nightly-<date>
# Anything typed there goes to the agent. Idle time while you're attached
# counts toward IDLE_STOP, so a long pause ends the run.
#
# The live checkout is never modified. The prompt and settings are read from
# it, so only reviewed, merged-and-pulled versions of them ever run.
#
# Logs: /root/gadget-nightly/logs/<date>.log (this script plus Claude's final
# summary).
#
# One-time setup, done by hand in a root terminal: accept the bypass-permissions
# disclaimer (IS_SANDBOX=1 claude --dangerously-skip-permissions) and trust
# /root/gadget-nightly/repo (run `claude` there once). Background sessions
# refuse to start without both.
set -uo pipefail

LIVE=/root/middle-earth-gadget-monitor
WORK=/root/gadget-nightly
REPO="$WORK/repo"
REPO_URL=https://github.com/dirac-institute/middle-earth-gadget-monitor.git
TIME_LIMIT_S=$((5 * 3600))
IDLE_STOP=10        # minutes
LOG_DAYS=60
NAME="nightly-$(date +%F)"

export HOME=/root
export PATH=/root/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin

mkdir -p "$WORK/logs"
exec >>"$WORK/logs/$(date +%F).log" 2>&1
exec 9>"$WORK/lock"
flock -n 9 || { echo "$(date -u +%FT%TZ) nightly: already running; exiting"; exit 0; }

log() { echo "$(date -u +%FT%TZ) nightly: $*"; }

# What is running now: container, image, and the checkout it was deployed from.
# Deploying from the clone changes the last field even if the images match.
live_state() {
    podman ps -a --filter label=idac.service=gadget-monitor \
        --format '{{.Names}} {{.ImageID}} {{.State}} {{index .Labels "com.docker.compose.project.working_dir"}}' \
        | sort
}

# A field of the newest background session called $NAME, "" if there is no
# such session, or "?" if the session list couldn't be read.
session_field() {
    claude agents --json --all 2>/dev/null | python3 -c '
import json, sys
try:
    sessions = json.load(sys.stdin)
except ValueError:
    print("?")
    sys.exit()
s = [x for x in sessions if x.get("kind") == "background" and x.get("name") == sys.argv[1]]
s.sort(key=lambda x: x.get("startedAt", 0))
print(s[-1].get(sys.argv[2], "") if s else "")' "$NAME" "$1"
}

# The last thing Claude said in the session: its summary of the night.
final_summary() {
    python3 - "/root/.claude/projects/-root-gadget-nightly-repo/$1.jsonl" <<'EOF'
import json, sys
last = ""
try:
    for line in open(sys.argv[1]):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") != "assistant":
            continue
        for b in e.get("message", {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "text" and b["text"].strip():
                last = b["text"].strip()
except OSError as err:
    last = f"(no transcript: {err})"
print(last)
EOF
}

finish() {
    if [ -n "${ID:-}" ]; then
        claude stop "$ID" >/dev/null 2>&1 && log "stopped session $ID"
    fi
    restore_live
}

restore_live() {
    if [ "$(live_state)" = "$BEFORE" ]; then
        log "live stack unchanged"
        return
    fi
    log "live stack changed; redeploying from $LIVE"
    (cd "$LIVE" && podman compose up -d --build --force-recreate) || log "ERROR: redeploy failed"
    sleep 60
    podman ps --filter label=idac.service=gadget-monitor --format '{{.Names}} {{.Status}}'
    [ "$(podman ps -q --filter label=idac.service=gadget-monitor | wc -l)" -eq 4 ] \
        || log "ERROR: not all four containers are up after the redeploy"
}

log "start"
find "$WORK/logs" -name '*.log' -mtime +"$LOG_DAYS" -delete

if [ ! -d "$REPO/.git" ]; then
    git clone -q "$REPO_URL" "$REPO" || { log "ERROR: clone failed"; exit 1; }
fi
cd "$REPO" || exit 1
git fetch -q --prune origin || { log "ERROR: fetch failed"; exit 1; }
if ! { git checkout -q --detach origin/main && git reset -q --hard origin/main && git clean -qfdx; }; then
    log "ERROR: could not reset the clone"
    exit 1
fi
cp "$LIVE/.env" .env

BEFORE="$(live_state)"
log "live stack before:"$'\n'"$BEFORE"
trap finish EXIT

log "starting session $NAME at $(git rev-parse --short HEAD)"
# IS_SANDBOX=1: Claude Code otherwise refuses to skip permission prompts as
# root. The deny rules in settings.json still apply.
IS_SANDBOX=1 claude --bg -n "$NAME" --permission-mode bypassPermissions \
    --settings "$LIVE/nightly/settings.json" "$(cat "$LIVE/nightly/prompt.md")" \
    || { log "ERROR: could not start the session"; exit 1; }
ID="$(session_field id)"
SID="$(session_field sessionId)"
[ -n "$ID" ] || { log "ERROR: session $NAME not found after starting it"; exit 1; }
log "session $ID; watch with: claude attach $NAME"

deadline=$(( $(date +%s) + TIME_LIMIT_S ))
idle=0
while sleep 60; do
    status="$(session_field status)"
    if [ -z "$status" ]; then
        log "session ended on its own"
        break
    elif [ "$status" = busy ]; then
        idle=0
    elif [ "$status" = "?" ]; then
        log "could not read the session list; still waiting"
    elif [ $((++idle)) -ge "$IDLE_STOP" ]; then
        log "session idle ($(session_field state)) for $IDLE_STOP minutes"
        break
    fi
    if [ "$(date +%s)" -ge "$deadline" ]; then
        log "time limit reached"
        break
    fi
done

log "final summary:"$'\n'"$(final_summary "$SID")"
