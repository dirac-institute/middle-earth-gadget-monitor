#!/bin/bash
# Nightly issue worker, run by cron as root (see nightly/crontab).
#
# Starts a background Claude Code session (named nightly-<date>-1) in a
# separate clone of the repo with nightly/prompt.md: it picks one actionable
# open issue (or review feedback on one of its own PRs), works on it, tests by
# deploying its branch to the live stack, and opens a PR. Once the session has
# sat idle for IDLE_STOP minutes, or the time limit is reached, it is stopped
# and the live stack is put back exactly as it was if anything about it
# changed.
#
# Then, while the night lasts, it starts another session (nightly-<date>-2,
# -3, ...) on a freshly reset clone if all of these hold:
#   - the last session's summary ended "NIGHTLY_RESULT: worked" (see
#     prompt.md), so there may be more to do;
#   - the plan has room for one more session (usage.py room);
#   - at least NEXT_MIN_LEFT minutes of the time limit are left;
#   - the live stack is back up after the last session;
#   - fewer than MAX_SESSIONS have run.
#
# Watch it, live or afterwards, in a full Claude Code window:
#   claude attach nightly-<date>-<n>
# Anything typed there goes to the agent. Idle time while you're attached
# counts toward IDLE_STOP, so a long pause ends the session.
#
# The live checkout is never modified. The prompt and settings are read from
# it, so only reviewed, merged-and-pulled versions of them ever run.
#
# Logs: /root/gadget-nightly/logs/<date>.log (this script plus each session's
# final summary). Plan usage snapshots: /root/gadget-nightly/usage.jsonl.
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
TIME_LIMIT_S=$((5 * 3600))   # for the whole night, all sessions together
IDLE_STOP=10        # minutes
USAGE_EVERY=10      # minutes between plan usage snapshots
NEXT_MIN_LEFT=60    # minutes; less than this left and no new session starts
MAX_SESSIONS=4
LOG_DAYS=60
DATE="$(date +%F)"

export HOME=/root
export PATH=/root/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin

mkdir -p "$WORK/logs"
exec >>"$WORK/logs/$DATE.log" 2>&1
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

# Claude's summary of the session: its last message that has a
# NIGHTLY_RESULT line, or else its last message (it sometimes adds an aside
# after the summary). The transcript is filed under the session's working
# directory, which is a worktree of the clone once the agent has entered one,
# so look in every project and take the newest.
final_summary() {
    python3 - "$1" <<'EOF'
import glob, json, os, sys
last = marked = ""
paths = glob.glob(f"/root/.claude/projects/*/{sys.argv[1]}.jsonl")
try:
    if not paths:
        raise OSError(f"{sys.argv[1]}.jsonl not found under /root/.claude/projects")
    for line in open(max(paths, key=os.path.getmtime)):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("type") != "assistant":
            continue
        for b in e.get("message", {}).get("content") or []:
            if isinstance(b, dict) and b.get("type") == "text" and b["text"].strip():
                last = b["text"].strip()
                if "NIGHTLY_RESULT:" in last:
                    marked = last
except OSError as err:
    last = f"(no transcript: {err})"
print(marked or last)
EOF
}

# Plan usage, recorded in $WORK/usage.jsonl (see usage.py).
usage_snap() {
    log "$(python3 "$LIVE/nightly/usage.py" snap "$NAME" "$1" 2>&1)"
}

# Stop the current session (if one is running) and record its end.
end_session() {
    [ -n "${ID:-}" ] || return
    claude stop "$ID" >/dev/null 2>&1 && log "stopped session $ID"
    usage_snap end
    ID=
}

finish() {
    end_session
    restore_live
    log "plan usage by session:"$'\n'"$(python3 "$LIVE/nightly/usage.py" report 2>&1)"
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
    # Expect every container that was running before the night to be up again,
    # rather than a fixed count that goes stale when a service is added.
    local want have
    want="$(grep -c ' running ' <<<"$BEFORE")"
    have="$(podman ps -q --filter label=idac.service=gadget-monitor | wc -l)"
    if [ "$have" -lt "$want" ]; then
        log "ERROR: $have containers up after the redeploy; $want were running before"
        return 1
    fi
}

# Put the clone back at origin/main, so each session starts clean. The
# agent's worktrees under .claude/worktrees are nested checkouts, which
# `git clean` leaves alone.
reset_clone() {
    if [ ! -d "$REPO/.git" ]; then
        git clone -q "$REPO_URL" "$REPO" || { log "ERROR: clone failed"; return 1; }
    fi
    cd "$REPO" || return 1
    git fetch -q --prune origin || { log "ERROR: fetch failed"; return 1; }
    if ! { git checkout -q --detach origin/main && git reset -q --hard origin/main && git clean -qfdx; }; then
        log "ERROR: could not reset the clone"
        return 1
    fi
    cp "$LIVE/.env" .env
}

# Run session number $1 to its end: start it, wait until it has been idle for
# IDLE_STOP minutes or the night's time limit is reached, then stop it and log
# its summary. Sets RESULT to the summary's NIGHTLY_RESULT value ("" if there
# is none, "timeout" if the time limit cut it off).
run_session() {
    NAME="nightly-$DATE-$1"
    RESULT=
    usage_snap start
    log "starting session $NAME at $(git rev-parse --short HEAD)"
    # IS_SANDBOX=1: Claude Code otherwise refuses to skip permission prompts as
    # root. The deny rules in settings.json still apply.
    IS_SANDBOX=1 claude --bg -n "$NAME" --permission-mode bypassPermissions \
        --settings "$LIVE/nightly/settings.json" "$(cat "$LIVE/nightly/prompt.md")" \
        || { log "ERROR: could not start the session"; return 1; }
    ID="$(session_field id)"
    local sid
    sid="$(session_field sessionId)"
    [ -n "$ID" ] || { log "ERROR: session $NAME not found after starting it"; return 1; }
    log "session $ID; watch with: claude attach $NAME"

    local idle=0 minutes=0 status summary
    while sleep 60; do
        [ $((++minutes % USAGE_EVERY)) -eq 0 ] && usage_snap sample
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
            RESULT=timeout
            break
        fi
    done

    summary="$(final_summary "$sid")"
    log "final summary of $NAME:"$'\n'"$summary"
    end_session
    if [ -z "$RESULT" ]; then
        RESULT="$(grep -o 'NIGHTLY_RESULT: *[a-z-]*' <<<"$summary" | tail -n 1 | sed 's/.*: *//')"
    fi
    return 0  # the session ran, whatever its result (grep fails if there's none)
}

log "start"
find "$WORK/logs" -name '*.log' -mtime +"$LOG_DAYS" -delete
deadline=$(( $(date +%s) + TIME_LIMIT_S ))

reset_clone || exit 1
BEFORE="$(live_state)"
log "live stack before:"$'\n'"$BEFORE"
trap finish EXIT

n=0
while :; do
    run_session $((++n)) || break
    log "session $n result: ${RESULT:-(none)}"
    [ "$RESULT" = worked ] || break
    if [ "$n" -ge "$MAX_SESSIONS" ]; then
        log "$MAX_SESSIONS sessions run; done for the night"
        break
    fi
    if [ $(( deadline - $(date +%s) )) -lt $((NEXT_MIN_LEFT * 60)) ]; then
        log "under $NEXT_MIN_LEFT minutes left; done for the night"
        break
    fi
    room="$(python3 "$LIVE/nightly/usage.py" room 2>&1)"
    ok=$?
    log "$room"
    [ "$ok" -eq 0 ] || break
    restore_live || { log "no more sessions tonight"; break; }
    reset_clone || break
done
