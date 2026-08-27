#!/usr/bin/env python3
"""Enrich the KEPT extraction rows with addresses + strict UTC shift columns.

Consumes `row_assignment.json` from schedule_shifts.py (per-row strict UTC slot,
keyed airport|trip|route_rank|time_bucket). Rows NOT in the assignment were
trimmed by the 30/hr cap and are excluded here (they live in dropped_routes.csv).

Purely additive on the survivors: routes, buckets, coordinates and local
hour/day columns are copied row-for-row from the baseline; we FILL the city-side
address (Nominatim cache) and ADD the UTC columns (always strictly in-window, so
no displacement column is needed). quality_summary is NOT copied here — realized
tiers come from recompute_realized_quality.py.

City side: PU -> dropoff is the city end; DO -> pickup is the city end.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

OUT_COLUMNS = (
    "city_id", "dayofweek", "hourofday",
    "dayofweek_utc", "hourofday_utc", "bpo_shift_slot",
    "pickup_address", "dropoff_address",
    "pickup_lat", "pickup_lng", "dropoff_lat", "dropoff_lng",
    "pu_airport_code", "do_airport_code",
    "time_bucket", "route_rank",
)
ROUND_DP = 6


def _key(lat, lng) -> str:
    return f"{round(float(lat), ROUND_DP)},{round(float(lng), ROUND_DP)}"


def _enrich(in_path: Path, trip: str, cache: dict, assign: dict,
            airport_addr: dict, airport_name: dict) -> tuple[list[dict], int]:
    rows, skipped = [], 0
    for r in csv.DictReader(in_path.open()):
        apt = (r.get("pu_airport_code") or r.get("do_airport_code") or "").strip()
        akey = f"{apt}|{trip}|{r['route_rank']}|{r['time_bucket']}"
        a = assign.get(akey)
        if a is None:          # trimmed by the cap
            skipped += 1
            continue
        apt_addr = airport_addr.get(apt, "")
        apt_label = (f"{airport_name.get(apt, apt)} — {apt_addr}"
                     if apt_addr else airport_name.get(apt, apt))
        out = dict(r)
        out["dayofweek_utc"] = a["day_utc"]
        out["hourofday_utc"] = a["hour_utc"]
        out["bpo_shift_slot"] = a["slot"]
        if trip == "PU":   # airport = pickup, city = dropoff
            out["pickup_address"] = apt_label
            out["dropoff_address"] = cache.get(_key(r["dropoff_lat"], r["dropoff_lng"]), "")
        else:              # airport = dropoff, city = pickup
            out["dropoff_address"] = apt_label
            out["pickup_address"] = cache.get(_key(r["pickup_lat"], r["pickup_lng"]), "")
        rows.append(out)
    return rows, skipped


def _write(path: Path, rows: list[dict]) -> None:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(OUT_COLUMNS))
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in OUT_COLUMNS})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline-dir", required=True, type=Path)
    ap.add_argument("--cache", required=True, type=Path)
    ap.add_argument("--airport-cache", required=True, type=Path)
    ap.add_argument("--airport-coords", required=True, type=Path)
    ap.add_argument("--assignment", required=True, type=Path,
                    help="row_assignment.json from schedule_shifts.py")
    ap.add_argument("--out-dir", required=True, type=Path)
    args = ap.parse_args(argv)

    args.out_dir.mkdir(parents=True, exist_ok=True)
    cache = json.loads(args.cache.read_text())
    assign = json.loads(args.assignment.read_text())
    airport_addr = json.loads(args.airport_cache.read_text())
    coords = json.loads(args.airport_coords.read_text())
    airport_name = {k: v.get("name", k) for k, v in coords.items() if isinstance(v, dict)}

    pu, sp_pu = _enrich(args.baseline_dir / "PU_extraction.csv", "PU", cache, assign,
                        airport_addr, airport_name)
    do, sp_do = _enrich(args.baseline_dir / "DO_extraction.csv", "DO", cache, assign,
                        airport_addr, airport_name)
    _write(args.out_dir / "PU_extraction.csv", pu)
    _write(args.out_dir / "DO_extraction.csv", do)
    print(f"enriched kept PU={len(pu)} DO={len(do)} (trimmed PU={sp_pu} DO={sp_do}) -> {args.out_dir}")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
