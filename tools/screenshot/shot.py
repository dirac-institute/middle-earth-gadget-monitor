#!/usr/bin/env python3
"""Screenshot a Grafana dashboard or one panel. Runs inside the image built
from this directory; use tools/screenshot.sh, which explains the arguments."""
import argparse, base64, json, os, re, sys, time, urllib.parse, urllib.request
from playwright.sync_api import sync_playwright

GRID_ROW_PX = 38  # Grafana's grid: 30 px per row plus an 8 px margin


def all_panels(panels):
    for p in panels:
        yield p
        yield from all_panels(p.get("panels", []))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("uid")
    ap.add_argument("panel", nargs="?", default="")
    ap.add_argument("--from", dest="frm")
    ap.add_argument("--to")
    ap.add_argument("--var", action="append", default=[])
    ap.add_argument("--width", type=int)
    ap.add_argument("--height", type=int)
    ap.add_argument("--theme", default="dark")
    ap.add_argument("--tz", default="America/Los_Angeles")
    ap.add_argument("--out", required=True)
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    a = ap.parse_args()

    headers = {}
    if os.environ.get("GRAFANA_AUTH"):
        headers["Authorization"] = "Basic " + base64.b64encode(os.environ["GRAFANA_AUTH"].encode()).decode()
    req = urllib.request.Request(f"{a.url}/api/dashboards/uid/{a.uid}", headers=headers)
    try:
        dash = json.load(urllib.request.urlopen(req, timeout=30))["dashboard"]
    except Exception as e:
        sys.exit(f"dashboard {a.uid}: {e}" + ("" if headers else " (Admin folder? try --admin)"))

    params = [("from", a.frm or dash.get("time", {}).get("from", "now-6h")),
              ("to", a.to or dash.get("time", {}).get("to", "now")), ("theme", a.theme)]
    params += [("var-" + k, x) for k, v in (s.split("=", 1) for s in a.var) for x in v.split(",")]
    if a.panel:
        panels = [p for p in all_panels(dash.get("panels", [])) if p.get("type") != "row"]
        match = ([p for p in panels if str(p["id"]) == a.panel]
                 or [p for p in panels if a.panel.lower() in p.get("title", "").lower()])
        if len(match) != 1:
            sys.exit(f"panel {a.panel!r} matches {len(match)} panels: "
                     + "; ".join(f"{p['id']} {p.get('title')}" for p in (match or panels)))
        p = match[0]
        print(f"panel {p['id']}: {p.get('title')}")
        url = f"{a.url}/d-solo/{a.uid}/_?" + urllib.parse.urlencode(params + [("panelId", p["id"])])
        w, h = a.width or 1000, a.height or 500
    else:
        # Panels below the viewport are never queried, so make the viewport as
        # tall as the dashboard (collapsed rows stay collapsed).
        bottom = max((p["gridPos"]["y"] + p["gridPos"]["h"] for p in dash.get("panels", [])), default=20)
        url = f"{a.url}/d/{a.uid}/_?" + urllib.parse.urlencode(params + [("kiosk", "true")])
        w, h = a.width or 1600, a.height or min(bottom * GRID_ROW_PX + 100, 20000)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": w, "height": h}, timezone_id=a.tz,
                                extra_http_headers=headers)
        page.goto(url, wait_until="networkidle", timeout=60000)
        # Wait until no panel is still loading, then a moment for canvases to paint.
        deadline = time.time() + 60
        while time.time() < deadline and page.locator('[aria-label="Panel loading bar"]').count():
            page.wait_for_timeout(250)
        page.wait_for_timeout(1000)
        # A single-panel page has a "Powered by Grafana" badge over the panel's
        # top-right corner; hide it so it can't cover data.
        page.evaluate("""() => { for (const e of document.querySelectorAll('span'))
            if (e.children.length < 2 && /^Powered by/.test(e.textContent.trim())) e.parentElement.style.visibility = 'hidden'; }""")
        page.screenshot(path=a.out, full_page=not a.panel)
        text = page.inner_text("body")
        browser.close()

    # A cheap hint, so a check often needs no look at the image at all.
    # (Query errors are easier to see with tools/panel_data.py.)
    n = len(re.findall(r"\bNo data\b", text))
    if n:
        print(f"'No data' shown {n} time(s)")
    print(f"wrote {a.out} ({w}x{h}) from {url}")


if __name__ == "__main__":
    main()
