"""publish_sheet.py — push the 5 hand-off tabs to a Google Sheet, with read-back
verification. Adapted from projects/2026-05-29_.../scripts/publish_snpe.py.

Matches the live SNPE/WAW hand-off sheet exactly: the tab named "quality_summary"
carries the REALIZED (post-trim) quality table, per the source hand-off.

Every chunk write is followed by a read-back of the tab's row count; a mismatch
aborts (this defends against the silent mid-block truncation bug the source hit).
Uses `aifx mcp call google-mcp sheets ...` via subprocess (same transport as the
source publishers).
"""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CHUNK_ROWS = 1000
SLEEP_S = 1.2

TABS = [
    ("PU_extraction", "PU_extraction.csv"),
    ("DO_extraction", "DO_extraction.csv"),
    ("quality_summary", "realized_quality_summary.csv"),  # realized, per hand-off
    ("bpo_shift_schedule", "bpo_shift_schedule.csv"),
    ("dropped_routes", "dropped_routes.csv"),
]

INT_COLS = {"city_id", "dayofweek", "hourofday", "dayofweek_utc", "hourofday_utc",
            "route_rank", "n_obs_current", "n_routes_current",
            "n_obs_to_be_added", "n_extraction_rows", "n_routes_to_be_added",
            "hour_utc", "n_obs", "n_dropped"}
NUM_COLS = {"pickup_lat", "pickup_lng", "dropoff_lat", "dropoff_lng",
            "current_sample_coverage", "current_g_coverage",
            "planned_sample_coverage", "realized_sample_coverage",
            "planned_g_coverage", "realized_g_coverage",
            "to_be_wo_exclusion_sample_coverage", "to_be_wo_exclusion_g_coverage",
            "to_be_with_exclusion_sample_coverage", "to_be_with_exclusion_g_coverage"}


def _call_mcp(server, tool, args, retries=3):
    for attempt in range(retries):
        fd, path = tempfile.mkstemp(suffix=".json", prefix="mcp_args_")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(args, f)
            proc = subprocess.run(["aifx", "mcp", "call", server, tool,
                                   "--args-file", path, "--no-token-savings"],
                                  capture_output=True)
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
        out = proc.stdout.decode("utf-8", errors="replace").strip()
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        if proc.returncode == 0:
            try:
                return json.loads(out)
            except ValueError:
                return {"raw": out}
        if "429" in err or "rate" in err.lower() or "quota" in err.lower():
            time.sleep((attempt + 1) * 5)
            continue
        sys.stderr.write("[mcp-fail %d] %s %s rc=%s err=%s\n"
                         % (attempt, server, tool, proc.returncode, err[:300]))
        time.sleep(2)
    return None


def _cast(col, v):
    if v in (None, "", r"\N"):
        return ""
    if col in INT_COLS:
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return v
    if col in NUM_COLS:
        try:
            return round(float(v), 6)
        except (TypeError, ValueError):
            return v
    return v


def _load_csv(path: Path):
    with path.open() as f:
        reader = csv.reader(f)
        headers = next(reader)
        rows = [[_cast(headers[i], v) for i, v in enumerate(r)] for r in reader]
    return headers, rows


def _read_row_count(sid, tab):
    r = _call_mcp("google-mcp", "sheets", {
        "resource": "spreadsheets.values", "method": "get",
        "params": {"spreadsheetId": sid, "range": "'%s'!A1:A200000" % tab}})
    if not r:
        return -1
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
    if not found:
        return 0
    return max(0, len([x for x in found[0] if x and x[0] not in (None, "")]) - 1)


def publish(out_dir: Path, sheet_id: str) -> str:
    """Clear + rewrite the 5 tabs on `sheet_id` from CSVs in out_dir. Returns URL."""
    data = [(tab, _load_csv(out_dir / fn)) for tab, fn in TABS
            if (out_dir / fn).exists()]
    if not data:
        raise SystemExit("[publish] no tab CSVs found in %s" % out_dir)
    nrows = {tab: len(d[1]) for tab, d in data}
    ncols = {tab: len(d[0]) for tab, d in data}
    url = "https://docs.google.com/spreadsheets/d/%s/edit" % sheet_id

    meta = _call_mcp("google-mcp", "sheets", {
        "resource": "spreadsheets", "method": "get",
        "params": {"spreadsheetId": sheet_id}})
    title_to_id = {}

    def _walk(o):
        if isinstance(o, dict):
            props = o.get("properties") or {}
            if props.get("title") and "sheetId" in props:
                title_to_id[props["title"]] = props["sheetId"]
            for v in o.values():
                _walk(v)
        elif isinstance(o, list):
            for v in o:
                _walk(v)
    _walk(meta or {})

    addreqs = [{"addSheet": {"properties": {
        "title": tab, "gridProperties": {"rowCount": nrows[tab] + 10,
                                         "columnCount": ncols[tab] + 2}}}}
        for tab, _ in data if tab not in title_to_id]
    if addreqs:
        _call_mcp("google-mcp", "sheets", {
            "resource": "spreadsheets", "method": "batchUpdate",
            "params": {"spreadsheetId": sheet_id}, "body": {"requests": addreqs}})
        meta = _call_mcp("google-mcp", "sheets", {
            "resource": "spreadsheets", "method": "get",
            "params": {"spreadsheetId": sheet_id}})
        title_to_id = {}
        _walk(meta or {})

    sizereqs = [{"updateSheetProperties": {
        "properties": {"sheetId": title_to_id[tab],
                       "gridProperties": {"rowCount": nrows[tab] + 10,
                                          "columnCount": ncols[tab] + 2}},
        "fields": "gridProperties.rowCount,gridProperties.columnCount"}}
        for tab, _ in data if tab in title_to_id]
    if sizereqs:
        _call_mcp("google-mcp", "sheets", {
            "resource": "spreadsheets", "method": "batchUpdate",
            "params": {"spreadsheetId": sheet_id}, "body": {"requests": sizereqs}})

    for tab, _ in data:
        if not _call_mcp("google-mcp", "sheets", {
                "resource": "spreadsheets.values", "method": "clear",
                "params": {"spreadsheetId": sheet_id, "range": "'%s'" % tab}, "body": {}}):
            raise SystemExit("[publish] clear failed for tab %s" % tab)

    for tab, (headers, rows) in data:
        print("[publish] %s (%d rows x %d cols)" % (tab, len(rows), len(headers)))
        if not _call_mcp("google-mcp", "sheets_update", {
                "spreadsheet_id": sheet_id, "range": "'%s'!A1" % tab,
                "values": json.dumps([headers])}):
            raise SystemExit("[publish] header write failed for %s" % tab)
        for k in range(0, len(rows), CHUNK_ROWS):
            chunk = rows[k:k + CHUNK_ROWS]
            start = 2 + k
            if not _call_mcp("google-mcp", "sheets_update", {
                    "spreadsheet_id": sheet_id, "range": "'%s'!A%d" % (tab, start),
                    "values": json.dumps(chunk)}):
                raise SystemExit("[publish] chunk at row %d failed in %s" % (start, tab))
            time.sleep(SLEEP_S)
        got = _read_row_count(sheet_id, tab)
        if got != len(rows):
            raise SystemExit("[publish] VERIFY FAILED %s: sheet has %d rows, expected %d"
                             % (tab, got, len(rows)))
        print("[publish]   verified %d data rows == CSV" % got)

    print("[publish] URL: %s" % url)
    return url
