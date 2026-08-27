#!/usr/bin/env python3
"""Reverse-geocode the city-side centroids of the SNPE pilot extraction CSVs.

City side = the non-airport end of each route:
  PU rows -> dropoff_lat/lng     DO rows -> pickup_lat/lng

Uses OSM Nominatim (free, public). Only bare lat/lng leave the machine.
Throttled >=1.1s/req, retry/backoff on 429/5xx, results persisted to a JSON
cache so reruns are free and the fetch is resumable. Anton approved external
Nominatim + full formatted address (display_name). Mirrors the approach in
projects/2026-06-04_remote-session-origins/scripts/geocode.py.

Usage:
  python3 geocode_addresses.py \
      --baseline-dir <.../baseline> \
      --cache <.../outputs/data/geocode_cache.json>
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

NOMINATIM = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "uber-airports-npi-pilot/1.0 (anton.tereshchuk; reverse-geocode pilot routes)"
SLEEP_S = 1.1
ROUND_DP = 6


def _key(lat: float, lng: float) -> str:
    return f"{round(float(lat), ROUND_DP)},{round(float(lng), ROUND_DP)}"


def _city_points(baseline_dir: Path) -> set[tuple[float, float]]:
    pts: set[tuple[float, float]] = set()
    for fn, (la, lo) in [
        ("PU_extraction.csv", ("dropoff_lat", "dropoff_lng")),
        ("DO_extraction.csv", ("pickup_lat", "pickup_lng")),
    ]:
        p = baseline_dir / fn
        if not p.exists():
            continue
        for r in csv.DictReader(p.open()):
            pts.add((round(float(r[la]), ROUND_DP), round(float(r[lo]), ROUND_DP)))
    return pts


def _fetch(lat: float, lng: float) -> str:
    qs = urllib.parse.urlencode({
        "format": "jsonv2", "lat": f"{lat}", "lon": f"{lng}",
        "zoom": "18", "addressdetails": "0",
    })
    req = urllib.request.Request(f"{NOMINATIM}?{qs}", headers={"User-Agent": USER_AGENT})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return (data.get("display_name") or "").strip()
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504):
                wait = 5 * (attempt + 1)
                print(f"  HTTP {e.code} -> backoff {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            raise
        except Exception as e:  # noqa: BLE001 - transient network
            wait = 5 * (attempt + 1)
            print(f"  {type(e).__name__}: {e} -> backoff {wait}s", file=sys.stderr)
            time.sleep(wait)
    return ""  # give up; left blank, surfaced in validation


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--baseline-dir", required=True, type=Path)
    ap.add_argument("--cache", required=True, type=Path)
    args = ap.parse_args(argv)

    args.cache.parent.mkdir(parents=True, exist_ok=True)
    cache: dict[str, str] = {}
    if args.cache.exists():
        cache = json.loads(args.cache.read_text())

    pts = _city_points(args.baseline_dir)
    todo = [(la, lo) for (la, lo) in pts if _key(la, lo) not in cache or not cache[_key(la, lo)]]
    print(f"{len(pts)} unique city points; {len(cache)} cached; {len(todo)} to fetch")

    for i, (la, lo) in enumerate(sorted(todo), 1):
        addr = _fetch(la, lo)
        cache[_key(la, lo)] = addr
        if i % 25 == 0 or i == len(todo):
            args.cache.write_text(json.dumps(cache, ensure_ascii=False, indent=0))
            print(f"  {i}/{len(todo)} fetched (cache saved)")
        time.sleep(SLEEP_S)

    args.cache.write_text(json.dumps(cache, ensure_ascii=False, indent=0))
    filled = sum(1 for (la, lo) in pts if cache.get(_key(la, lo)))
    print(f"done: {filled}/{len(pts)} points have an address")
    return 0


if __name__ == "__main__":
    sys.exit(main())
