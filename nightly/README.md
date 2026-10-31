# Nightly agent

Every night at 1 AM, `run.sh` starts a background Claude Code session in a
separate clone of this repo (`/root/gadget-nightly/repo`). The session:

1. addresses review feedback on its own open `nightly/*` PRs, or else picks
   one actionable open issue;
2. builds and tests the change, deploying its branch to the live stack to
   test it;
3. pushes a branch, opens a PR, and comments on the issue.

The session is stopped after 10 idle minutes or 5 hours. The wrapper then
redeploys from `/root/middle-earth-gadget-monitor` if the running containers
changed, so the live stack ends up as it was. Expect the odd metrics blip
between 1 and 6 AM.

| File | What it is |
|---|---|
| `run.sh` | The cron wrapper: clone reset, session start, idle/time limit, restore |
| `prompt.md` | The nightly task given to Claude |
| `settings.json` | Deny rules: no merge, close, delete, push to main, or edits to the live checkout |
| `crontab` | The cron line (not installed automatically) |

Testing rules and the GitHub rules the agent follows are in `../CLAUDE.md`.
The prompt and settings are read from the live checkout, so only versions
you've merged and pulled there ever run.

## Watching a session

Sessions are named `nightly-<date>`, e.g. `nightly-2026-10-31`.

```bash
claude agents                       # list sessions; pick one to open it
claude agents --json --all          # the same as JSON, including finished ones
claude attach nightly-2026-10-31    # open one (part of the name is enough)
claude logs nightly-2026-10-31      # print recent output without attaching
```

Attaching gives you a normal Claude Code window with full scrollback. You can
attach while it is running or the next morning; a stopped session reopens
with its whole conversation.

**It is not read-only.** Anything you type goes to the agent, which you can
use to steer it. Idle time while you are attached still counts toward the 10
idle minutes, so a long pause ends the run.

### Leaving without stopping it

- **Ctrl+Z**: back to your shell.
- **←** (left arrow, with the input box empty): back to the `claude agents`
  list.

Either way the session keeps running. Don't use `/exit` or Ctrl+C to leave:
those can end or interrupt the session itself.

## Logs

`/root/gadget-nightly/logs/<date>.log` has the wrapper's steps (live stack
before, session start and stop, restore) and Claude's final summary. Logs are
kept 60 days.

## Setup

One time, in a root terminal on the VM. Background sessions refuse to start
without both:

```bash
IS_SANDBOX=1 claude --dangerously-skip-permissions   # accept the disclaimer, then /exit
mkdir -p /root/gadget-nightly
git clone https://github.com/dirac-institute/middle-earth-gadget-monitor.git /root/gadget-nightly/repo
cd /root/gadget-nightly/repo && claude               # accept the trust prompt, then /exit
```

`gh` must be logged in as root (see `../CLAUDE.md`, GitHub).

Do a supervised run first and watch it:

```bash
/root/middle-earth-gadget-monitor/nightly/run.sh
```

Then install the cron line:

```bash
crontab -l | cat - /root/middle-earth-gadget-monitor/nightly/crontab | crontab -
```

To pause the nightly runs, remove the line with `crontab -e`. To end a run
early, `claude stop <id>` (the id is in `claude agents` and the log). The
wrapper notices within 10 minutes and restores the live stack.
