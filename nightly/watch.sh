#!/bin/bash
# Follow the newest nightly session as it runs: Claude's messages, the tools
# it calls, and the start of each result. Ctrl-C to stop.
#
# run.sh's log only gets Claude's final summary; this is the live view.
set -euo pipefail

DIR=/root/.claude/projects/-root-gadget-nightly-repo
# Session files are uuid-named, so ls is safe here.
# shellcheck disable=SC2012
f="$(ls -t "$DIR"/*.jsonl 2>/dev/null | head -1)"
[ -n "$f" ] || { echo "no nightly session transcripts in $DIR" >&2; exit 1; }
echo "following $f" >&2

tail -n +1 -f "$f" | python3 -u -c '
import json, sys
W = 300
for line in sys.stdin:
    try:
        e = json.loads(line)
    except ValueError:
        continue
    content = e.get("message", {}).get("content")
    if not isinstance(content, list):
        continue
    for b in content:
        t = b.get("type")
        if t == "text" and b["text"].strip():
            print("\n" + b["text"].strip())
        elif t == "tool_use":
            i = b.get("input", {})
            arg = i.get("command") or i.get("file_path") or i.get("pattern") or json.dumps(i)
            print("\n> " + b["name"] + ": " + arg[:W])
        elif t == "tool_result":
            r = b.get("content")
            if isinstance(r, list):
                r = " ".join(x.get("text", "") for x in r if isinstance(x, dict))
            r = (r or "").strip().replace("\n", " | ")
            print("  = " + r[:W])
'
