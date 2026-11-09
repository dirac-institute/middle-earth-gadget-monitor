# Nightly agent

Every night at 1 AM, `run.sh` starts a background Claude Code session in a
separate clone of this repo (`/root/gadget-nightly/repo`). The session:

1. addresses review feedback on its own open `nightly/*` PRs, or else picks
   one actionable open issue;
2. builds and tests the change, deploying its branch to the live stack to
   test it;
3. pushes a branch, opens a PR, and comments on the issue.

The session is stopped after 10 idle minutes. The wrapper then redeploys
from `/root/middle-earth-gadget-monitor` if the running containers changed,
so the live stack ends up as it was. If the session says it got something
done, the wrapper starts another one (see "More than one session a night"),
up to 5 hours in all. Expect the odd metrics blip between 1 and 6 AM.

| File | What it is |
|---|---|
| `nightly.conf` | This project's settings: live checkout, repo, work directory, session names, live stack and how to restore it |
| `run.sh` | The cron wrapper: clone reset, sessions, idle/time limit, restore |
| `usage.py` | Plan usage snapshots, the per-session report, and the room check (see "Plan usage") |
| `prompt.md` | The nightly task given to Claude, after a short section `run.sh` writes from `nightly.conf` |
| `settings.json` | Deny rules: no merge, close, delete or push to main. `run.sh` adds rules against touching the live checkout |
| `crontab` | The cron line (not installed automatically) |

Testing rules and the GitHub rules the agent follows are in `../CLAUDE.md`.
The prompt and settings are read from the live checkout, so only versions
you've merged and pulled there ever run.

## Watching a session

Sessions are named `<SESSION_PREFIX>-<date>-<n>` (`nightly` here), e.g. `nightly-2026-11-05-1` for the
night's first. (Before November 2026 there was one a night, `nightly-<date>`.)

```bash
claude agents                         # list sessions; pick one to open it
claude agents --json --all            # the same as JSON, including finished ones
claude attach nightly-2026-11-05-1    # open one
claude logs nightly-2026-11-05-1      # print recent output without attaching
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
before, each session's start and stop, restores, why it did or didn't start
another session) and each session's final summary. Logs are kept 60 days.

## More than one session a night

Each session ends its summary with a line saying how it went (the wording is
in `prompt.md`, "Finish"):

| Line | Meaning | Another session? |
|---|---|---|
| `NIGHTLY_RESULT: worked` | It handled feedback or made progress on an issue | If the rest below allows |
| `NIGHTLY_RESULT: nothing-to-do` | Nothing was actionable | No |
| `NIGHTLY_RESULT: blocked` | Something outside the repo stopped it (said in the summary) | No |
| no line, or the time limit cut it off | | No |

After a `worked` session, `run.sh` starts the next one only if all of these
hold, and logs which one didn't:

- fewer than 4 sessions (`MAX_SESSIONS`) have run tonight;
- at least 60 minutes (`NEXT_MIN_LEFT`) of the 5-hour limit are left;
- `usage.py room` says the plan has room (next section);
- the live stack came back up after the redeploy.

The next session gets a freshly reset clone at `origin/main`. It sees the
earlier sessions' PRs and comments the same way it sees older ones, so it
moves on to other work.

## Plan usage

`run.sh` records how much of the Claude plan each session uses, and uses
that to decide whether there is room for another. `usage.py` reads the same
numbers `/usage` shows in Claude Code (percent used of the 5-hour window and
of the week) and appends a snapshot to `/root/gadget-nightly/usage.jsonl`
before each session starts, every 10 minutes while it runs, and after it
stops. The usage is for the whole account, so anyone else using it during the
night shows up too.

Each log ends with a per-run table; to see it any time:

```bash
python3 /root/middle-earth-gadget-monitor/nightly/usage.py report      # last 14 sessions
python3 /root/middle-earth-gadget-monitor/nightly/usage.py report 60
python3 /root/middle-earth-gadget-monitor/nightly/usage.py room        # would another fit now?
```

There is a row per session: each `start` snapshot begins a new one, so
same-name runs started by hand are counted separately. `used` is percentage
points of 5-hour window the session took, summed across a window reset if
the session spans one; `week` is the rise in the weekly figure. The 1 AM run
usually opens a fresh window, which resets 5 hours later, around the time the
night's limit is reached. If the OAuth token has expired by 1 AM, the start
snapshot fails and the report counts from the first sample, a few minutes
into the session (marked "no start snapshot").

`room` takes the newest snapshot (the `end` of the session just finished) and
answers yes only if both of these fit:

- 5-hour window: now + the most any of the last 14 sessions used + 15 points;
- week: now + the most any of them added + 30 points.

The week's buffer is large on purpose: the night shouldn't eat into what the
maintainer needs for the rest of the week. Both buffers can be changed with
`NIGHTLY_FIVE_HOUR_BUFFER` and `NIGHTLY_WEEK_BUFFER`. If the newest snapshot
failed, the answer is no.

The endpoint is the one Claude Code calls for `/usage`, not a documented API.
A failed snapshot is logged and recorded as an error; it never stops a run.

## Using it in another project

Everything in this directory except `nightly.conf` and `crontab` is the same
for every project.

1. Copy `nightly/` into the other repo.
2. Edit `nightly.conf`:
   - `LIVE`, `REPO_URL`, `WORK`: its live checkout, its GitHub URL, and a
     work directory of its own.
   - `SESSION_PREFIX`: must differ from this project's if both run under the
     same Claude login (the same user on the same machine). `run.sh` finds its
     sessions by name.
   - `LIVE_LABEL`, `RESTORE_CMD`, `COPY_FILES`: how its live stack is found
     and put back, and which untracked files a clone needs. Leave
     `LIVE_LABEL` empty if there is no live stack; then nothing is restored.
3. Fix the path in `crontab`, and pick a time that doesn't overlap another
   project's run.
4. Write the repo a `CLAUDE.md`. The prompt leans on it for everything
   project-specific: how to test, what must never be deployed live, the
   GitHub rules (see this repo's `CLAUDE.md` for the shape).
5. On GitHub: a fine-grained token for that repo (Contents, Issues, Pull
   requests: read/write), and a ruleset on `main` (require a pull request,
   block force pushes, restrict deletions, no bypass). `gh` holds one token
   per user, so a second project under the same user needs a token that
   covers both repos.
6. Do the setup below with that project's paths.

**Plan usage is per Claude login.** Projects running as the same user on the
same machine share one plan: `usage.py` measures the whole account, so each
project's `room` check sees the other's use, and their nights compete for the
same budget. For a separate budget, run the other project as a different user
(or with its own `CLAUDE_CONFIG_DIR`) logged in to a different account.

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

To pause the nightly runs, remove the line with `crontab -e`. To end a
session early, `claude stop <id>` (the id is in `claude agents` and the log).
The wrapper notices within a minute and restores the live stack. A session
stopped before its summary has no `NIGHTLY_RESULT` line, so no other session
starts after it. To end the whole night, also remove the line from cron, or
stop each new session as it appears.
