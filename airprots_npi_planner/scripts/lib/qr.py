#!/usr/bin/env python3
"""
qr.py — run a single .sql file (with {{param}} substitution) through queryrunner-mcp
via `aifx`, write results to outputs/<name>_results.csv, and print a small preview +
the DataCentral / QueryBuilder UUID.

call_mcp / run_query pattern vendored from
projects/2026-06-15_airport-rsp-simulation-v2/scripts/run_queries.py (proven). Stdlib only.

usage:
  qr.py <sqlfile> [--out <csvpath>] [--start YYYY-MM-DD] [--end YYYY-MM-DD]
                  [--airports "'LHR','CDG'"] [--preview N]
"""
from __future__ import print_function
import csv, json, os, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(os.path.dirname(HERE))   # scripts/lib -> scripts -> asset root
OUT = os.path.join(PROJ, "outputs")


def call_mcp(server, tool, args=None):
    af = ot = None
    try:
        fd, af = tempfile.mkstemp(suffix=".json"); os.close(fd)
        fd, ot = tempfile.mkstemp(suffix=".json"); os.close(fd)
        with open(af, "w") as f:
            json.dump(args or {}, f)
        subprocess.check_output(
            ["aifx", "mcp", "call", server, tool,
             "--args-file", af, "--no-token-savings", "-o", ot],
            stderr=subprocess.STDOUT, timeout=1800)
        with open(ot) as f:
            return f.read()
    finally:
        for p in (af, ot):
            if p and os.path.exists(p):
                os.remove(p)


MAX_FETCH_ROWS = 20000000  # hard cap on escalation — see fetch_all_rows() below


def fetch_all_rows(uuid, label, fetch):
    """get_execution_results' fetch_rows is a hard LIMIT with no offset/total-count field, so a
    result that exactly fills the requested fetch_rows is indistinguishable from a result that was
    silently truncated. Re-querying the same (already-completed) execution_uuid at a larger
    fetch_rows costs no additional query execution — it re-reads the cached result — so escalate
    fetch_rows whenever the returned row count exactly matches what was requested, until it stops
    being an exact match (i.e. we've captured the true tail) or we hit MAX_FETCH_ROWS.
    """
    while True:
        res = json.loads(call_mcp("queryrunner-mcp", "get_execution_results",
                                  {"execution_uuids": [uuid], "fetch_rows": fetch}))
        status = res[0].get("status")
        rows = res[0].get("rows") or []
        cols = res[0].get("columns") or []
        if status == "completed_success" and len(rows) == fetch:
            if fetch >= MAX_FETCH_ROWS:
                sys.exit(
                    "[%s] result still exactly fills fetch_rows=%d at the hard cap "
                    "(MAX_FETCH_ROWS=%d) — refusing to silently truncate; uuid=%s"
                    % (label, fetch, MAX_FETCH_ROWS, uuid))
            new_fetch = fetch * 4
            print("[%s] rows==fetch_rows (%d) — result may be truncated, re-fetching same "
                  "uuid at fetch_rows=%d (no re-execution cost)" % (label, fetch, new_fetch),
                  file=sys.stderr)
            fetch = new_fetch
            continue
        return status, rows, cols, res


def run_query(sql, label, fetch=300000, retries=2):
    last = ("never_ran", [], [], None)
    for attempt in range(retries + 1):
        if attempt > 0:
            print("[%s] retry %d/%d" % (label, attempt + 1, retries + 1), file=sys.stderr)
            time.sleep(15)
        resp = json.loads(call_mcp("queryrunner-mcp", "execute_query", {"query": sql}))
        uuid = resp["execution_uuid"]
        print("[%s] submitted %s" % (label, uuid), file=sys.stderr)
        start = time.time()
        while True:
            st = json.loads(call_mcp("queryrunner-mcp", "check_execution_status",
                                     {"execution_uuids": [uuid]}))
            el = time.time() - start
            print("[%s]   [%5.1fs] %s" % (label, el, st[0].get("status")), file=sys.stderr)
            if st[0]["is_complete"]:
                break
            time.sleep(8)
        status, rows, cols, res = fetch_all_rows(uuid, label, fetch)
        print("[%s] status=%s rows=%d cols=%d uuid=%s"
              % (label, status, len(rows), len(cols), uuid), file=sys.stderr)
        last = (status, rows, cols, uuid)
        if status == "completed_success":
            return last
        err = {k: v for k, v in res[0].items() if k not in ("rows", "columns")}
        print("[%s] err: %s" % (label, json.dumps(err)[:1500]), file=sys.stderr)
    return last


def write_csv(path, cols, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            if isinstance(r, dict):
                w.writerow([r.get(c, "") for c in cols])
            else:
                w.writerow(r)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    sqlfile = sys.argv[1]
    args = sys.argv[2:]

    def opt(name, default=None):
        return args[args.index(name) + 1] if name in args else default

    subs = {
        "{{start_date}}": opt("--start", "2026-05-18"),
        "{{end_date}}":   opt("--end", "2026-06-15"),
        "{{airports}}":   opt("--airports", "'LHR','CDG','LGW','MAN','MAD','AMS','ORY','BCN'"),
    }
    preview = int(opt("--preview", "20"))
    out = opt("--out", os.path.join(
        OUT, os.path.basename(sqlfile).replace(".sql", "") + "_results.csv"))

    sql = open(sqlfile).read()
    # queryrunner-mcp allows read-only statements only — strip 'set session ...;' hints.
    sql = "\n".join(ln for ln in sql.splitlines()
                    if not ln.strip().lower().startswith("set session"))
    for k, v in subs.items():
        sql = sql.replace(k, v)

    status, rows, cols, uuid = run_query(sql, os.path.basename(sqlfile))
    if status != "completed_success":
        sys.exit("FAILED status=%s" % status)
    if not os.path.isdir(OUT):
        os.makedirs(OUT)
    write_csv(out, cols, rows)
    print("\n[write] %s  (%d rows)" % (out, len(rows)))
    print("[DataCentral]  https://datacentral.uberinternal.com/queryrunner/queries/%s/overview" % uuid)
    print("\ncols: %s" % cols)
    for r in rows[:preview]:
        if isinstance(r, dict):
            print([r.get(c) for c in cols])
        else:
            print(r)


if __name__ == "__main__":
    main()
