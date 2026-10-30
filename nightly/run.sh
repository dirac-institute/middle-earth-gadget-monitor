#!/bin/bash
# Nightly issue worker, run by cron as root (see nightly/crontab).
#
# Starts a headless Claude Code session in a separate clone of the repo with
# nightly/prompt.md: it picks one actionable open issue (or review feedback on
# one of its own PRs), works on it, tests by deploying its branch to the live
# stack, and opens a PR. When the session ends, for any reason, the live stack
# is put back exactly as it was if anything about it changed.
#
# The live checkout is never modified. The prompt and settings are read from
# it, so only reviewed, merged-and-pulled versions of them ever run.
#
# Logs: /root/gadget-nightly/logs/<date>.log (this script plus Claude's final
# summary). The full session transcript is under
# /root/.claude/projects/-root-gadget-nightly-repo/.
set -uo pipefail

LIVE=/root/middle-earth-gadget-monitor
WORK=/root/gadget-nightly
REPO="$WORK/repo"
REPO_URL=https://github.com/dirac-institute/middle-earth-gadget-monitor.git
TIME_LIMIT=5h
LOG_DAYS=60

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
trap restore_live EXIT

log "running claude (limit $TIME_LIMIT) at $(git rev-parse --short HEAD)"
# IS_SANDBOX=1: Claude Code otherwise refuses to skip permission prompts as
# root. The deny rules in settings.json still apply.
IS_SANDBOX=1 timeout -k 5m "$TIME_LIMIT" claude -p "$(cat "$LIVE/nightly/prompt.md")" \
    --permission-mode bypassPermissions \
    --settings "$LIVE/nightly/settings.json"
log "claude exited with status $?"
