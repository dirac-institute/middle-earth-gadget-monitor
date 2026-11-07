#!/usr/bin/env python3
"""Set folder permissions in Grafana, and keep the UW NetID team in sync.

Started in the background by entrypoint.sh, with the admin password on stdin.
File provisioning can't set folder permissions, so once Grafana answers:

- Admin (per-user usage): remove every grant (org admins always keep access).
- UW NetID: View for the "UW NetID" team only.

then confirm an anonymous request is refused for each. The permissions persist
in grafana.db; this re-applies them on every start in case someone edited them
in the UI.

Adding users to teams at sign-in (team sync) is a Grafana Enterprise feature,
so instead this keeps every user other than the admin in the team, checking
every 10 seconds. With sign-up off except through UW NetID, those are the
NetID users. A first-time user may wait that long for the folder.
"""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = f"http://127.0.0.1:{os.environ.get('GF_SERVER_HTTP_PORT', '3000')}/api"
ADMIN_USER = os.environ["GF_SECURITY_ADMIN_USER"]
TEAM = "UW NetID"

# (folder uid, a dashboard in it to test anonymously, folder name, grants)
FOLDERS = [
    ("gadget-admin", "gadget-users", "Admin", []),
    ("gadget-uw", "gadget-uw-status", "UW NetID", [{"team": TEAM, "permission": 1}]),
]

SETUP_TRIES, SETUP_WAIT = 60, 5
SYNC_EVERY = 10


def log(msg):
    print(f"gadget: {msg}", flush=True)


def warn(msg):
    print(f"gadget: WARNING {msg}", file=sys.stderr, flush=True)


class Grafana:
    def __init__(self, user, password):
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.auth = f"Basic {token}"

    def call(self, method, path, body=None):
        """JSON request as the admin. Raises OSError (incl. HTTPError) on failure."""
        req = urllib.request.Request(
            API + path, method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={"Authorization": self.auth, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read() or b"null")

    def team_id(self, name):
        """The team's id, creating the team if it doesn't exist yet."""
        found = self.call("GET", "/teams/search?name=" + urllib.parse.quote(name))["teams"]
        if found:
            return found[0]["id"]
        return self.call("POST", "/teams", {"name": name})["teamId"]


def anonymous_status(dashboard_uid):
    try:
        with urllib.request.urlopen(f"{API}/dashboards/uid/{dashboard_uid}", timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except OSError as e:
        return str(e)


def set_permissions(gf):
    """Apply FOLDERS once. Returns the UW NetID team's id."""
    teams = {}
    for folder, _, _, grants in FOLDERS:
        items = []
        for g in grants:
            if g["team"] not in teams:
                teams[g["team"]] = gf.team_id(g["team"])
            items.append({"teamId": teams[g["team"]], "permission": g["permission"]})
        gf.call("POST", f"/folders/{folder}/permissions", {"items": items})
    return teams[TEAM]


def setup(gf):
    """Retry set_permissions while Grafana starts, then report what anonymous sees."""
    team = None
    for _ in range(SETUP_TRIES):
        time.sleep(SETUP_WAIT)
        try:
            team = set_permissions(gf)
            break
        except (OSError, KeyError, ValueError):
            continue
    for _, dashboard, name, _ in FOLDERS:
        code = anonymous_status(dashboard)
        if code == 200:
            warn(f"{name} folder is still visible anonymously")
        else:
            log(f"{name} folder restricted (anonymous gets {code})")
    if team is None:
        warn("could not set folder permissions; restricted dashboards may be public")
    return team


def sync_team(gf, team):
    """Add every user other than the admin to the team."""
    members = {m["userId"] for m in gf.call("GET", f"/teams/{team}/members")}
    users = {u["userId"] for u in gf.call("GET", "/org/users") if u["login"] != ADMIN_USER}
    for uid in sorted(users - members):
        gf.call("POST", f"/teams/{team}/members", {"userId": uid})
        log(f"added user {uid} to the {TEAM} team")


def main():
    gf = Grafana(ADMIN_USER, sys.stdin.read().rstrip("\n"))
    team = setup(gf)
    if team is None:
        return
    while True:
        time.sleep(SYNC_EVERY)
        try:
            sync_team(gf, team)
        except (OSError, KeyError, ValueError):
            pass


if __name__ == "__main__":
    main()
