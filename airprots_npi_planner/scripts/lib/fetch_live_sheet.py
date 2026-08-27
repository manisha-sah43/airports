#!/usr/bin/env python3
"""Pull all 5 tabs of the live SNPE pilot sheet (19UAk...) to local CSVs.

Ground-truth source for the frozen non-WAW airports + the format we must mirror.
Read-only on the sheet. Uses the same aifx mcp google-mcp sheets pattern as
publish_waw.py.
"""
from __future__ import print_function
import csv, json, os, subprocess, sys, tempfile, time

SHEET_ID = "19UAkSYujxgFDz39IBMexUFXKs_nxU5yAmGFKONDqivo"
TABS = ["PU_extraction", "DO_extraction", "quality_summary",
        "bpo_shift_schedule", "dropped_routes"]
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "..", "inputs", "live_19UAk")


def call_mcp(server, tool, args, retries=4):
    for attempt in range(retries):
        fd, path = tempfile.mkstemp(suffix=".json", prefix="mcp_args_")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(args, f)
            proc = subprocess.run(["aifx", "mcp", "call", server, tool,
                                   "--args-file", path, "--no-token-savings"],
                                  capture_output=True)
        finally:
            try: os.unlink(path)
            except OSError: pass
        out = proc.stdout.decode("utf-8", errors="replace").strip()
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        if proc.returncode == 0:
            try: return json.loads(out)
            except ValueError: return {"raw": out}
        sys.stderr.write("[mcp-fail %d] %s rc=%s err=%s\n"
                         % (attempt, tool, proc.returncode, err[:300]))
        time.sleep(3)
    return None


def find_values(r):
    found = []
    def walk(o):
        if isinstance(o, dict):
            if isinstance(o.get("values"), list):
                found.append(o["values"])
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(r)
    return found[0] if found else []


def main():
    if not os.path.isdir(OUT):
        os.makedirs(OUT)
    for tab in TABS:
        r = call_mcp("google-mcp", "sheets", {
            "resource": "spreadsheets.values", "method": "get",
            "params": {"spreadsheetId": SHEET_ID,
                       "range": "'%s'!A1:Z200000" % tab}})
        if r is None:
            sys.exit("read failed: %s" % tab)
        vals = find_values(r)
        path = os.path.join(OUT, tab + ".csv")
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            for row in vals:
                w.writerow(row)
        ncol = len(vals[0]) if vals else 0
        print("%-20s rows=%d (incl header) cols=%d -> %s"
              % (tab, len(vals), ncol, path))


if __name__ == "__main__":
    main()
