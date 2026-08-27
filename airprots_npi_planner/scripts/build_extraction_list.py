"""build_extraction_list.py — emit BPO observation-request rows for resolved cuts.

Adapted (demand-free) from shared/scripts/coverage_curve_v5/build_extraction_list.py.
The load-bearing logic is vendored verbatim:
  * `_covered_buckets`   — the "already-existing observations" exclusion rule
                           (a (hex, time_bucket) with >= min_obs existing obs is
                           skipped so the BPO isn't asked to collect it again).
  * `_emit_extraction_rows` — one output row per surviving (route, time_bucket):
                           the v5 observation grain (NOT day-of-week exploded).
  * quality via lib.tiering (score-rounded tiers).

Two deviations from the source, both to stay demand-free (this asset vendors the
coverage curves + dashboard baseline, and pulls only fresh observations — it does
NOT carry the 206 MB per-airport demand table):
  1. `current_*` baseline is read from the vendored dashboard `current.csv`
     (the same baseline the npi-coverage-v5 dashboard shows) instead of being
     recomputed from demand. It can differ by <1pp from a same-day demand
     recompute; it never affects the observation list itself.
  2. `to_be_with_exclusion_*` == `to_be_wo_exclusion_*` (read from the routes.json
     snapshot at N): a conservative lower bound on the true (new ∪ existing)
     union, since the union covers at least as much as the new routes alone.
     Existing observations still reduce WHAT MUST BE COLLECTED via the exclusion
     in `_emit_extraction_rows`.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pandas as pd

from lib.curve_config import (
    BUCKET_TO_DAYS, TIME_BUCKET_MID_HOUR, SHEET_COLUMNS, QUALITY_COLUMNS)
from lib.quality_lookup import classify
from lib.tiering import snapshot_at, quality_from_snapshot
from observations import filter_competitor


# ---------------------------------------------------------------- baseline ----

def load_current_baseline(current_csv: Path) -> dict[tuple[str, str], dict]:
    """Parse the dashboard current.csv into {(airport, trip): {sample, geo, n_routes}}."""
    out: dict[tuple[str, str], dict] = {}
    if not current_csv.exists():
        return out
    with current_csv.open() as f:
        for r in csv.DictReader(f):
            apt = (r.get("airport_code") or "").strip().upper()
            if not apt:
                continue
            for trip in ("PU", "DO"):
                def _f(col):
                    v = r.get(col, "")
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        return 0.0
                out[(apt, trip)] = {
                    "sample": _f(f"sample_coverage_today_{trip}"),
                    "geo": _f(f"geo_coverage_today_{trip}"),
                    "n_routes": int(_f(f"n_unique_routes_today_{trip}")),
                }
    return out


# ------------------------------------------------------------ exclusion --------

def _covered_buckets(obs_df: pd.DataFrame, trip: str,
                     min_obs: int) -> set[tuple[str, str]]:
    """{(hex_id, time_bucket): sum(num_observations) >= min_obs} for this trip."""
    if obs_df.empty:
        return set()
    sub = obs_df[obs_df["trip_type"] == trip]
    if sub.empty:
        return set()
    grp = (sub.groupby(["hex_id", "time_bucket"], as_index=False)
              ["num_observations"].sum())
    grp = grp[grp["num_observations"] >= min_obs]
    return set(zip(grp["hex_id"].astype(str), grp["time_bucket"].astype(str)))


def _airport_address(airport_code: str, coords: dict) -> str:
    name = coords.get("name") or f"{airport_code} Airport"
    return f"{name} ({airport_code})"


def _emit_extraction_rows(routes: list[dict], trip: str, scen: str,
                          airport: str, city_id: int, coords: dict,
                          covered: set[tuple[str, str]]
                          ) -> tuple[list[dict], int, int]:
    """Explode routes -> candidate sheet rows. Returns (rows, n_dropped, n_emitted)."""
    out: list[dict] = []
    n_dropped = 0
    n_emitted = 0
    air_lat = float(coords["lat"])
    air_lng = float(coords["lng"])
    air_addr = _airport_address(airport, coords)
    pbcol = f"picked_buckets_{scen}"
    for r in routes:
        rank = int(r["rank"])
        hex_id = str(r["hex_id"])
        centroid = r.get("centroid") or {}
        c_lat = centroid.get("lat")
        c_lng = centroid.get("lng")
        if c_lat is None or c_lng is None:
            continue
        for bucket in r.get(pbcol, []):
            if (hex_id, bucket) in covered:
                n_dropped += 1
                continue
            mid_hour = TIME_BUCKET_MID_HOUR.get(bucket)
            days = BUCKET_TO_DAYS.get(bucket, ())
            if mid_hour is None or not days:
                continue
            rep_day = days[0]
            n_emitted += 1
            if trip == "PU":
                out.append({
                    "city_id": city_id, "dayofweek": rep_day, "hourofday": mid_hour,
                    "pickup_address": air_addr, "dropoff_address": "",
                    "pickup_lat": air_lat, "pickup_lng": air_lng,
                    "dropoff_lat": float(c_lat), "dropoff_lng": float(c_lng),
                    "pu_airport_code": airport, "do_airport_code": "",
                    "time_bucket": bucket, "route_rank": rank,
                })
            else:
                out.append({
                    "city_id": city_id, "dayofweek": rep_day, "hourofday": mid_hour,
                    "pickup_address": "", "dropoff_address": air_addr,
                    "pickup_lat": float(c_lat), "pickup_lng": float(c_lng),
                    "dropoff_lat": air_lat, "dropoff_lng": air_lng,
                    "pu_airport_code": "", "do_airport_code": airport,
                    "time_bucket": bucket, "route_rank": rank,
                })
    return out, n_dropped, n_emitted


# ------------------------------------------------------------- per-cut ---------

def process_cut(cut: dict, routes_dir: Path, obs_df: pd.DataFrame,
                coords_map: dict, city_id_map: dict[str, int],
                baseline: dict, min_obs: int,
                competitor_map_path: Path) -> tuple[list[dict], dict | None]:
    """Returns (extraction_rows, quality_row) for one (airport, trip, scen, N) cut."""
    airport = cut["airport"].upper()
    trip = cut["trip_type"].upper()
    scen = cut["scenario"].lower()
    n_routes = int(cut["n_routes"])

    rj = routes_dir / airport / "routes.json"
    if not rj.exists():
        raise SystemExit(f"[build] missing routes.json for {airport}: {rj}")
    rjson = json.loads(rj.read_text())

    coords = coords_map.get(airport)
    if not coords:
        raise SystemExit(f"[build] missing coords for {airport}")
    city_id = city_id_map.get(airport)
    if city_id is None:
        raise SystemExit(f"[build] missing city_id for {airport}")

    # Existing observations for this airport, filtered to its canonical competitor.
    obs_air = obs_df[obs_df["airport_code"].astype(str).str.upper() == airport].copy() \
        if not obs_df.empty else pd.DataFrame()
    if not obs_air.empty:
        obs_air = filter_competitor(obs_air, airport, competitor_map_path)

    routes_block = rjson["trip_types"][trip]
    ordered = routes_block["ordered_routes"][:n_routes]
    covered = _covered_buckets(obs_air, trip, min_obs)
    rows, n_dropped, n_emitted = _emit_extraction_rows(
        ordered, trip, scen, airport, city_id, coords, covered)

    n_obs_to_be_added = n_emitted
    n_routes_to_be_added = len({r["route_rank"] for r in rows})
    if len(rows) != n_obs_to_be_added:
        raise SystemExit(f"[build] row/obs mismatch {airport}/{trip}: "
                         f"{len(rows)} != {n_obs_to_be_added}")
    if n_routes_to_be_added > n_routes:
        raise SystemExit(f"[build] route mismatch {airport}/{trip}: "
                         f"{n_routes_to_be_added} > {n_routes}")

    # current baseline from dashboard current.csv
    base = baseline.get((airport, trip), {"sample": 0.0, "geo": 0.0, "n_routes": 0})
    cur_s, cur_g = base["sample"], base["geo"]
    cur_q = classify(cur_s, cur_g).quality
    n_routes_current = base["n_routes"]
    # n_obs_current: weekly (4-week obs total / 4), matching the source convention.
    if not obs_air.empty:
        sub = obs_air[obs_air["trip_type"] == trip]
        n_obs_current = int(round(float(sub["num_observations"].sum()) / 4.0))
    else:
        n_obs_current = 0

    # to-be quality from the routes.json snapshot at N (== curves.csv / dashboard).
    snaps = (routes_block.get("snapshots") or {}).get(scen) or []
    snap = snapshot_at(snaps, n_routes)
    wo_s, wo_g, wo_qoverall, wo_qtime = quality_from_snapshot(snap)

    qrow = {
        "airport_code": airport, "trip_type": trip,
        "current_sample_coverage": round(cur_s, 4),
        "current_g_coverage": round(cur_g, 4),
        "npi_quality": cur_q,
        "to_be_wo_exclusion_sample_coverage": round(wo_s, 4),
        "to_be_wo_exclusion_g_coverage": round(wo_g, 4),
        "to_be_wo_exclusion_time_NPI_quality": wo_qtime,
        "to_be_wo_exclusion_NPI_quality": wo_qoverall,
        "to_be_with_exclusion_sample_coverage": round(wo_s, 4),
        "to_be_with_exclusion_g_coverage": round(wo_g, 4),
        "to_be_with_exclusion_time_NPI_quality": wo_qtime,
        "to_be_with_exclusion_NPI_quality": wo_qoverall,
        "n_obs_current": n_obs_current,
        "n_routes_current": n_routes_current,
        "n_obs_to_be_added": n_obs_to_be_added,
        "n_extraction_rows": len(rows),
        "n_routes_to_be_added": n_routes_to_be_added,
    }
    return rows, qrow


def write_csv(path: Path, rows: list[dict], columns: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(columns))
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in columns})


def build(cuts: list[dict], routes_dir: Path, obs_df: pd.DataFrame,
          coords_map: dict, city_id_map: dict[str, int],
          current_csv: Path, min_obs: int, competitor_map_path: Path,
          out_dir: Path) -> dict:
    """Run all cuts; write PU/DO_extraction.csv + quality_summary.csv. Returns stats."""
    baseline = load_current_baseline(current_csv)
    pu_rows, do_rows, quality_rows = [], [], []
    for cut in cuts:
        rows, qrow = process_cut(cut, routes_dir, obs_df, coords_map,
                                 city_id_map, baseline, min_obs, competitor_map_path)
        if qrow is None:
            continue
        (pu_rows if cut["trip_type"].upper() == "PU" else do_rows).extend(rows)
        quality_rows.append(qrow)
    write_csv(out_dir / "PU_extraction.csv", pu_rows, SHEET_COLUMNS)
    write_csv(out_dir / "DO_extraction.csv", do_rows, SHEET_COLUMNS)
    write_csv(out_dir / "quality_summary.csv", quality_rows, QUALITY_COLUMNS)
    return {"pu": len(pu_rows), "do": len(do_rows), "cuts": len(quality_rows)}
