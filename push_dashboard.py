"""
Validate a generated dashboard's panel SQL against the live Grafana
Postgres data source, then upload it and read it back.

    python push_dashboard.py live-operations.json
    python push_dashboard.py live-operations.json --var voyage=123456 --check-only
    python push_dashboard.py live-operations.json --from 2026-08-12T08:00:00Z --to 2026-08-12T14:00:00Z

Reads GRAFANA_URL / GRAFANA_TOKEN from the environment or .env (gitignored).

If the dashboard JSON has an "x-row-variables" key ({repeat_var: [names]}),
those variables are moved onto the row repeated over repeat_var after
upload, via Grafana's v2 dashboard API (see apply_row_variables).

Template variables are resolved the way Grafana would with every picker
left at its default "All": each variable's own query is run in order
(later variables can reference earlier ones), and the results are
substituted. --var name=value pins one to a single value instead, which
is useful for the single-voyage live board. Queries go through
/api/ds/query, so Grafana macros ($__timeTo() etc.) expand exactly as
they do in a panel, using the dashboard's default time range.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

VAR_RE = re.compile(r"\$\{(\w+)(?::(\w+))?\}|\$(\w+)")


def load_env(path=".env"):
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def api(method, path, body=None):
    req = urllib.request.Request(
        os.environ["GRAFANA_URL"].rstrip("/") + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": "Bearer " + os.environ["GRAFANA_TOKEN"],
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except ValueError:
            return e.code, {"message": str(e)}


def substitute(sql, values):
    def repl(m):
        name = m.group(1) or m.group(3)
        if name.startswith("__") or name not in values:
            return m.group(0)  # Grafana macro or unknown: leave for Grafana
        vals = values[name]
        fmt = m.group(2)
        # Grafana's SQL default (no :format) leaves a single value raw (the
        # panel SQL quotes it itself) and only quotes multi-value lists.
        if fmt == "sqlstring" or (fmt is None and len(vals) != 1):
            return ",".join("'" + v.replace("'", "''") + "'" for v in vals) or "''"
        return ",".join(vals)

    return VAR_RE.sub(repl, sql)


def run_sql(ds, sql, time_from, time_to):
    status, res = api("POST", "/api/ds/query", {
        "from": time_from,
        "to": time_to,
        "queries": [{"refId": "A", "datasource": ds, "rawSql": sql, "format": "table"}],
    })
    r = (res or {}).get("results", {}).get("A", {})
    if status != 200 or r.get("error"):
        return None, r.get("error") or (res or {}).get("message") or f"HTTP {status}"
    frames = r.get("frames", [])
    rows = sum(len(f["data"]["values"][0]) if f["data"]["values"] else 0 for f in frames)
    cols = [fld["name"] for f in frames for fld in f["schema"]["fields"]]
    first_col = frames[0]["data"]["values"][0] if frames and frames[0]["data"]["values"] else []
    return {"rows": rows, "cols": cols, "first_col": first_col}, None


def iter_panels(panels):
    for p in panels:
        yield p
        yield from iter_panels(p.get("panels", []))


def resolve_vars(dash, pinned, t_from, t_to, verbose=True):
    """Run each query variable in order (later ones can reference earlier
    ones), as Grafana would with every picker left on "All". Returns
    (values, error_count)."""
    values, failed = {}, 0
    for v in dash.get("templating", {}).get("list", []):
        name = v["name"]
        if name in pinned:
            values[name] = [pinned[name]]
            if verbose:
                print(f"var  {name:<15} pinned = {pinned[name]}")
            continue
        if v.get("type") != "query":
            continue
        q = v["query"]["rawSql"] if isinstance(v["query"], dict) else v["query"]
        res, err = run_sql(v["datasource"], substitute(q, values), t_from, t_to)
        if err:
            failed += 1
            print(f"var  {name:<15} ERROR {err}")
            values[name] = []
            continue
        values[name] = [str(x) for x in res["first_col"]]
        if verbose:
            print(f"var  {name:<15} {len(values[name])} values")
    return values, failed


def apply_row_variables(uid, row_vars):
    """Move dashboard variables onto the repeated row that owns them, via
    Grafana's v2 dashboard API (classic JSON has no row-scoped variables).
    A row-scoped variable is re-queried per repeated copy of the row, with
    that copy's value of the repeat variable — e.g. a per-voyage truck list.
    """
    path = f"/apis/dashboard.grafana.app/v2/namespaces/default/dashboards/{uid}"
    status, d = api("GET", path)
    if status != 200:
        print(f"v2 fetch failed: HTTP {status} {d}")
        sys.exit(1)
    spec = d["spec"]
    for repeat_var, names in row_vars.items():
        moved = [v for v in spec["variables"] if v["spec"]["name"] in names]
        spec["variables"] = [v for v in spec["variables"] if v["spec"]["name"] not in names]
        rows = [r for r in spec["layout"]["spec"].get("rows", [])
                if (r["spec"].get("repeat") or {}).get("value") == repeat_var]
        if not rows:
            print(f"No row repeating over ${repeat_var}; can't scope {names}")
            sys.exit(1)
        rows[0]["spec"]["variables"] = moved
    body = {"apiVersion": d["apiVersion"], "kind": "Dashboard",
            "metadata": {"name": d["metadata"]["name"],
                         "resourceVersion": d["metadata"]["resourceVersion"]},
            "spec": spec}
    status, res = api("PUT", path, body)
    if status != 200:
        print(f"v2 update failed: HTTP {status} {res}")
        sys.exit(1)
    _, d = api("GET", path)
    scoped = {(r["spec"].get("repeat") or {}).get("value"): [v["spec"]["name"] for v in r["spec"].get("variables", [])]
              for r in d["spec"]["layout"]["spec"].get("rows", []) if r["spec"].get("variables")}
    print(f"Row-scoped variables: {scoped}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("dashboard")
    ap.add_argument("--var", action="append", default=[], help="name=value (repeatable)")
    ap.add_argument("--from", dest="t_from", help="override time range start, e.g. 2026-08-12T08:00:00Z")
    ap.add_argument("--to", dest="t_to", help="override time range end")
    ap.add_argument("--check-only", action="store_true", help="validate, don't upload")
    args = ap.parse_args()

    load_env()
    with open(args.dashboard, encoding="utf-8") as f:
        dash = json.load(f)
    dash = dash.get("dashboard", dash)
    row_vars = dash.pop("x-row-variables", {})
    t_from = args.t_from or dash.get("time", {}).get("from", "now-6h")
    t_to = args.t_to or dash.get("time", {}).get("to", "now")
    pinned = dict(v.split("=", 1) for v in args.var)

    values, failed = resolve_vars(dash, pinned, t_from, t_to)

    row_repeat = None  # repeat variable of the row the current panel sits in
    for p in iter_panels(dash.get("panels", [])):
        if p.get("type") == "row":
            row_repeat = p.get("repeat")
            continue
        # A repeated row/panel sees one value of its repeat variable per
        # copy; validate against the first. Inside a repeated row,
        # variables that depend on the row's value are re-resolved with it
        # pinned, as Grafana does for row-scoped variables.
        pv = values
        if row_repeat and values.get(row_repeat):
            pv, _ = resolve_vars(dash, {**pinned, row_repeat: values[row_repeat][0]},
                                 t_from, t_to, verbose=False)
        pv = dict(pv)
        if p.get("repeat") and pv.get(p["repeat"]):
            pv[p["repeat"]] = pv[p["repeat"]][:1]
        for t in p.get("targets", []):
            if "rawSql" not in t:
                continue
            res, err = run_sql(t.get("datasource") or p["datasource"],
                               substitute(t["rawSql"], pv), t_from, t_to)
            label = f"#{p.get('id')} {p.get('title', '')[:40]!r} [{t.get('refId')}]"
            if err:
                failed += 1
                print(f"FAIL {label}\n     {err}")
            else:
                flag = "EMPTY" if res["rows"] == 0 else "ok   "
                print(f"{flag} {label} rows={res['rows']} cols={res['cols']}")

    if failed:
        print(f"\n{failed} query error(s); not uploading.")
        sys.exit(1)
    if args.check_only:
        return

    status, res = api("POST", "/api/dashboards/db", {"dashboard": {**dash, "id": None},
                                                     "overwrite": True})
    if status != 200:
        print(f"\nUpload failed: HTTP {status} {res}")
        sys.exit(1)
    print(f"\nUploaded v{res['version']}: {os.environ['GRAFANA_URL'].rstrip('/')}{res['url']}")

    _, back = api("GET", f"/api/dashboards/uid/{dash['uid']}")
    got = len(list(iter_panels(back["dashboard"].get("panels", []))))
    want = len(list(iter_panels(dash.get("panels", []))))
    print(f"Read back: {got}/{want} panels")

    if row_vars:
        apply_row_variables(dash["uid"], row_vars)


if __name__ == "__main__":
    main()
