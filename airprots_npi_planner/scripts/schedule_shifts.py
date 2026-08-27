#!/usr/bin/env python3
"""Strict 30-obs/hour shift assignment + minimal-quality-loss route trim.

Each baseline extraction row = ONE (route-hex, time_bucket) cell that must be
collected at its TRUE local time (strict occasions — no displacement). The BPO
can collect <=30 observations per hour-of-week (one global office, single week,
168 hours). Total demand (3886) exceeds what fits strictly under the cap (3459),
so 427 cells must be trimmed.

We choose the trim to minimise NPI quality loss: drop the LEAST important routes
(highest route_rank) in the over-subscribed occasions. Because every row of a
given (airport, trip, time_bucket) shares the same eligible UTC cells, the
placeable subsets form a transversal matroid; greedy by importance (route_rank
ascending) with augmenting-path b-matching is therefore OPTIMAL — it places the
maximum number of cells AND concentrates the drops on the highest ranks.

Outputs (into --out-dir):
  row_assignment.json   {airport|trip|rank|bucket -> {day_utc,hour_utc,slot}} KEPT
  dropped_routes.csv    the trimmed rows (airport,trip,route_rank,time_bucket,...)
  bpo_shift_schedule.csv one row per staffed (day,hour) cell (<=30) + breakdown

Deterministic (no Date/random): summer-2026 offsets from zoneinfo at a fixed
reference instant; rows processed in a fixed sort order.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.setrecursionlimit(10000)

REF = datetime(2026, 6, 15, 12, 0)  # summer 2026 (DST in effect for Europe)
CAP = 30

PERIOD_HOURS = {
    "night": [1, 2, 3, 4, 5, 6], "morning": [7, 8, 9, 10, 11],
    "day": [12, 13, 14, 15, 16, 17], "evening": [18, 19, 20],
    "late_evening": [21, 22, 23, 0],
}
WD = (1, 2, 3, 4, 5)
WE = (6, 7)
DAY3 = {1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat", 7: "Sun"}


def _period(b: str) -> str:
    for p in ("wkd_", "wked_", "wrek_"):
        if b.startswith(p):
            return b[len(p):]
    return b


def _dayclass_days(b: str) -> tuple[int, ...]:
    return WD if b.startswith("wkd_") else WE


def _offset_hours(tz: str) -> int:
    return int(REF.replace(tzinfo=ZoneInfo(tz)).utcoffset().total_seconds() // 3600)


def _strict_utc(b: str, off: int) -> list[int]:
    """Same-day-class UTC hours strictly inside the bucket's local window."""
    return sorted({h - off for h in PERIOD_HOURS[_period(b)] if 0 <= (h - off) <= 23})


def _load_rows(baseline_dir: Path) -> list[dict]:
    rows = []
    for fn, code in [("PU_extraction.csv", "pu_airport_code"),
                     ("DO_extraction.csv", "do_airport_code")]:
        p = baseline_dir / fn
        if not p.exists():
            continue
        trip = "PU" if fn.startswith("PU") else "DO"
        for r in csv.DictReader(p.open()):
            apt = (r.get("pu_airport_code") or r.get("do_airport_code") or "").strip()
            rows.append({
                "airport": apt, "trip": trip,
                "route_rank": int(r["route_rank"]),
                "time_bucket": r["time_bucket"],
            })
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline-dir", required=True, type=Path)
    ap.add_argument("--airport-coords", required=True, type=Path)
    ap.add_argument("--out-dir", required=True, type=Path)
    args = ap.parse_args(argv)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    coords = json.loads(args.airport_coords.read_text())
    rows = _load_rows(args.baseline_dir)
    offsets = {r["airport"]: _offset_hours(coords[r["airport"]]["timezone"]) for r in rows}

    # Eligible UTC cells per row (sorted for determinism).
    for r in rows:
        days = _dayclass_days(r["time_bucket"])
        hours = _strict_utc(r["time_bucket"], offsets[r["airport"]])
        r["cells"] = [(d, h) for d in days for h in hours]

    # Greedy b-matching by importance: route_rank ascending (most important first).
    order = sorted(range(len(rows)),
                   key=lambda i: (rows[i]["route_rank"], rows[i]["airport"],
                                  rows[i]["trip"], rows[i]["time_bucket"]))
    seated_in: dict[tuple[int, int], list[int]] = {}   # cell -> [row idx]
    assign: dict[int, tuple[int, int]] = {}            # row idx -> cell

    def try_seat(ri: int, visited: set) -> bool:
        # Standard Kuhn augmenting path for b-matching: each eligible cell is
        # marked visited once; if full, try to relocate one of its occupants
        # into a still-unvisited cell. Depth is bounded by the number of cells.
        for cell in rows[ri]["cells"]:
            if cell in visited:
                continue
            visited.add(cell)
            occ = seated_in.setdefault(cell, [])
            if len(occ) < CAP:
                occ.append(ri)
                assign[ri] = cell
                return True
            for rj in occ:
                if try_seat(rj, visited):       # rj moved to another cell
                    occ.remove(rj)
                    occ.append(ri)
                    assign[ri] = cell
                    return True
        return False

    dropped: list[int] = []
    for ri in order:
        if not try_seat(ri, set()):
            dropped.append(ri)

    kept = [i for i in range(len(rows)) if i in assign]
    # Sanity: cap respected.
    maxload = max((len(v) for v in seated_in.values()), default=0)
    assert maxload <= CAP, f"cap violated: {maxload}"

    # Emit row_assignment.json (kept) + dropped_routes.csv.
    out_map = {}
    for i in kept:
        d, h = assign[i]
        r = rows[i]
        key = f"{r['airport']}|{r['trip']}|{r['route_rank']}|{r['time_bucket']}"
        out_map[key] = {"day_utc": d, "hour_utc": h,
                        "slot": f"{DAY3[d]} {h:02d}:00Z"}
    (args.out_dir / "row_assignment.json").write_text(
        json.dumps(out_map, ensure_ascii=False, indent=1))

    dropped.sort(key=lambda i: (rows[i]["airport"], rows[i]["trip"],
                                rows[i]["time_bucket"], rows[i]["route_rank"]))
    with (args.out_dir / "dropped_routes.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["airport", "trip", "route_rank", "time_bucket", "reason"])
        for i in dropped:
            r = rows[i]
            w.writerow([r["airport"], r["trip"], r["route_rank"], r["time_bucket"],
                        "cap_full_strict_window"])

    # bpo_shift_schedule.csv: one row per staffed (day,hour) cell.
    with (args.out_dir / "bpo_shift_schedule.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["day", "hour_utc", "slot", "n_obs", "breakdown"])
        for cell in sorted(k for k, v in seated_in.items() if v):
            d, h = cell
            occ = seated_in[cell]
            bd = {}
            for i in occ:
                r = rows[i]
                bd[f"{r['airport']}/{r['trip']}/{r['time_bucket']}"] = \
                    bd.get(f"{r['airport']}/{r['trip']}/{r['time_bucket']}", 0) + 1
            brk = "; ".join(f"{k}:{v}" for k, v in sorted(bd.items()))
            w.writerow([DAY3[d], h, f"{DAY3[d]} {h:02d}:00Z", len(occ), brk])

    # Stats.
    days_used = sorted({d for (d, h), v in seated_in.items() if v})
    perapt_drop = {}
    for i in dropped:
        k = f"{rows[i]['airport']}/{rows[i]['trip']}"
        perapt_drop[k] = perapt_drop.get(k, 0) + 1
    print(f"rows={len(rows)} kept={len(kept)} dropped={len(dropped)} "
          f"({100*len(dropped)/len(rows):.1f}%)")
    print(f"staffed cells={sum(1 for v in seated_in.values() if v)} "
          f"max cell load={maxload} days={[DAY3[d] for d in days_used]}")
    print("drops by airport:", {k: perapt_drop[k] for k in sorted(perapt_drop)})
    return 0


if __name__ == "__main__":
    sys.exit(main())
