#!/usr/bin/env python3
"""Show what data each panel of a Grafana dashboard would draw, as text.

    tools/panel_data.py <dashboard-uid> [title-filter] [options]

    tools/panel_data.py gadget-hosts-overview "GPU util"
    tools/panel_data.py gadget-hosts-overview --from now-1h --var host=gondor
    tools/panel_data.py gadget-users --admin

For every panel whose title contains the filter (case-insensitive), runs the
panel's own queries through Grafana's query API, the way the browser does,
and prints one line per series: its labels, how many points it has and how
many are non-null, and min/max/last. "NO DATA" is what a viewer would see as
"No data". Repeated panels (repeat: host) are expanded per value. Collapsed
rows are included.

Template variables are resolved as the dashboard would by default (All, for
multi-value variables), unless set with --var, and written into queries as
Grafana writes them (allValue for All, regex-escaped values). Only
label_values(metric, label) queries are supported, which is all these
dashboards use.

Stdlib only; talks to Grafana on 127.0.0.1:8080 as the anonymous viewer, or
with --admin as admin (password read from shire with sudo -u astro-gadget),
which the Admin folder needs.
"""
import argparse, base64, json, math, re, subprocess, sys, urllib.error, urllib.request

PW_FILE = "/service/shire/gadget-monitor/secrets/grafana-admin-password"


class Grafana:
    def __init__(self, url, auth=None):
        self.url, self.auth = url.rstrip("/"), auth

    def call(self, path, body=None):
        req = urllib.request.Request(self.url + path, data=None if body is None else json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        if self.auth:
            req.add_header("Authorization", "Basic " + base64.b64encode(self.auth.encode()).decode())
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            # ds/query reports per-query errors with a non-2xx status but a normal body.
            text = e.read().decode(errors="replace")
            try:
                return json.loads(text)
            except ValueError:
                sys.exit(f"{path}: HTTP {e.code}: {text[:300]}")

    def query(self, queries, frm, to):
        return self.call("/api/ds/query", {"queries": queries, "from": frm, "to": to}).get("results", {})


def all_panels(panels):
    for p in panels:
        yield p
        yield from all_panels(p.get("panels", []))


def frames_series(frames):
    """(labels, display name, values, instant?) for every value field in a list of data frames."""
    for f in frames:
        fields, values = f["schema"]["fields"], f.get("data", {}).get("values", [])
        instant = f["schema"].get("meta", {}).get("custom", {}).get("resultType") == "vector"
        for i, fld in enumerate(fields):
            if fld.get("type") == "time" or i >= len(values):
                continue
            name = fld.get("config", {}).get("displayNameFromDS") or fld.get("name", "")
            yield fld.get("labels") or {}, name, values[i], instant


def resolve_vars(g, dash, frm, to, overrides):
    """{name: [values]} for each template variable, and {name: allValue} for those left at All."""
    out, all_text = {}, {}
    for v in dash.get("templating", {}).get("list", []):
        name = v["name"]
        if name in overrides:
            vals = overrides[name].split(",")
        else:
            cur = v.get("current", {}).get("value")
            cur = cur if isinstance(cur, list) else [cur] if cur else []
            if cur and "$__all" not in cur:
                vals = cur
            else:
                q = v.get("query")
                q = q.get("query") if isinstance(q, dict) else q
                m = re.fullmatch(r"\s*label_values\((.+),\s*(\w+)\)\s*", q or "")
                if not m:
                    sys.exit(f"variable {name}: unsupported query {q!r}; set it with --var {name}=...")
                res = g.query([{"refId": "v", "datasource": v.get("datasource") or {"uid": "victoriametrics"},
                                "expr": f"group by ({m[2]}) ({m[1]})", "instant": True, "range": False}], frm, to)
                vals = sorted({lab.get(m[2]) for lab, *_ in frames_series(res.get("v", {}).get("frames", []))} - {None})
                if v.get("allValue"):
                    all_text[name] = v["allValue"]
        out[name] = vals
    return out, all_text


def fmt_values(vals, multi):
    """A variable's values as Grafana's Prometheus datasource writes them into a query."""
    if not multi:
        return vals[0]
    # Multi-value and All variables are regex-escaped even when only one value is
    # selected (and in each copy of a repeated panel). The backslash is doubled
    # because it sits inside a PromQL string literal.
    esc = [re.sub(r"([$^*{}\[\]+?.()|])", r"\\\\\1", x.replace("\\", "\\\\\\\\")) for x in vals]
    return esc[0] if len(esc) == 1 else "(" + "|".join(esc) + ")"


def substitute(s, values):
    for name in sorted(values, key=len, reverse=True):
        s = s.replace("${" + name + "}", values[name]).replace("$" + name, values[name])
    return s


def duration_ms(s):
    m = re.fullmatch(r"[<>]?(\d+)(ms|s|m|h|d)", s.strip())
    if not m:
        sys.exit(f"can't parse interval {s!r}")
    return int(m[1]) * {"ms": 1, "s": 1000, "m": 60000, "h": 3600000, "d": 86400000}[m[2]]


def num(x):
    if x == 0:
        return "0"
    a = abs(x)
    return f"{x:.3g}" if 1e-3 <= a < 1e6 else f"{x:.2e}"


def describe(values):
    good = [v for v in values if isinstance(v, (int, float)) and not math.isnan(v)]
    s = f"{len(values)} pts, {len(good)} non-null"
    if good:
        s += f", min {num(min(good))} max {num(max(good))} last {num(good[-1])}"
    return s


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("uid", help="dashboard uid, e.g. gadget-hosts-overview")
    ap.add_argument("title", nargs="?", default="", help="only panels whose title contains this")
    ap.add_argument("--from", dest="frm", help="start, e.g. now-6h (default: the dashboard's)")
    ap.add_argument("--to", help="end (default: the dashboard's)")
    ap.add_argument("--var", action="append", default=[], metavar="NAME=V1[,V2]", help="set a template variable")
    ap.add_argument("--admin", action="store_true", help="log in as admin (needed for the Admin folder)")
    ap.add_argument("--url", default="http://127.0.0.1:8080")
    ap.add_argument("--max-points", type=int, default=1000, help="like the panel's width in pixels")
    ap.add_argument("--series", type=int, default=20, help="series to list per query before summarizing")
    a = ap.parse_args()

    auth = None
    if a.admin:
        pw = subprocess.run(["sudo", "-u", "astro-gadget", "cat", PW_FILE], capture_output=True, text=True, timeout=30)
        if pw.returncode:
            sys.exit(f"can't read {PW_FILE}: {pw.stderr.strip()}")
        auth = "admin:" + pw.stdout.strip()
    g = Grafana(a.url, auth)
    got = g.call(f"/api/dashboards/uid/{a.uid}")
    if "dashboard" not in got:
        sys.exit(f"dashboard {a.uid}: {got.get('message', got)}" + (" (Admin folder? try --admin)" if "ermission" in str(got) and not a.admin else ""))
    dash = got["dashboard"]
    frm = a.frm or dash.get("time", {}).get("from", "now-6h")
    to = a.to or dash.get("time", {}).get("to", "now")
    overrides = dict(v.split("=", 1) for v in a.var)
    var_vals, all_text = resolve_vars(g, dash, frm, to, overrides)
    multi = {v["name"]: v.get("multi") or v.get("includeAll") for v in dash.get("templating", {}).get("list", [])}
    print(f"{dash.get('title')} ({a.uid}), {frm} to {to}; "
          + ", ".join(f"{k}={','.join(v)}" for k, v in var_vals.items()))

    shown = 0
    for p in all_panels(dash.get("panels", [])):
        if p.get("type") == "row" or not p.get("targets"):
            continue
        rep = p.get("repeat")
        # A repeated panel is drawn once per value, with the variable set to that one value.
        copies = [{rep: [x]} for x in var_vals.get(rep, [])] if rep else [{}]
        for fixed in copies:
            # All is sent as the variable's allValue (e.g. .*) if it has one, as Grafana does.
            vals = {k: all_text[k] if k in all_text and k not in fixed else fmt_values(fixed.get(k, v), multi.get(k))
                    for k, v in var_vals.items()}
            title = substitute(p.get("title", ""), {k: ",".join(fixed.get(k, v)) for k, v in var_vals.items()})
            if a.title.lower() not in title.lower():
                continue
            shown += 1
            print(f"\n[{p['id']}] {title} ({p['type']})")
            queries = []
            for t in p["targets"]:
                if t.get("hide"):
                    continue
                q = {k: v for k, v in t.items() if k not in ("expr",)}
                q["expr"] = substitute(t.get("expr", ""), vals)
                q["datasource"] = t.get("datasource") or p.get("datasource") or {"uid": "victoriametrics"}
                q.setdefault("maxDataPoints", a.max_points)
                if p.get("interval"):
                    # The panel's minimum step. Without intervalMs the backend falls back to
                    # the datasource's timeInterval (5m), coarser than what Grafana draws.
                    q["intervalMs"] = duration_ms(p["interval"])
                queries.append(q)
            if not queries:
                print("  (no queries)")
                continue
            res = g.query(queries, frm, to)
            any_data = False
            for q in queries:
                r = res.get(q["refId"], {})
                if r.get("error"):
                    print(f"  {q['refId']}: ERROR {r['error']}")
                    continue
                series = list(frames_series(r.get("frames", [])))
                legend = q.get("legendFormat") or ""
                if not series:
                    print(f"  {q['refId']} {legend!r}: no series")
                    continue
                for lab, name, values, instant in series[:a.series]:
                    any_data = any_data or any(v is not None for v in values)
                    labs = ", ".join(f'{k}="{v}"' for k, v in sorted(lab.items()) if k != "__name__")
                    print(f"  {q['refId']}{' (instant)' if instant else ''} {name!r} {{{labs}}}: {describe(values)}")
                if len(series) > a.series:
                    rest = sum(1 for _, _, v, _ in series[a.series:] if any(x is not None for x in v))
                    print(f"  {q['refId']} ... and {len(series) - a.series} more series ({rest} with data)")
                    any_data = any_data or rest > 0
            if not any_data:
                print("  NO DATA")
    if not shown:
        sys.exit(f"no panel title contains {a.title!r}")


if __name__ == "__main__":
    main()
