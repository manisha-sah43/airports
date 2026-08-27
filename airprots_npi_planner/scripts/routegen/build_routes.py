"""build_routes.py — in-asset route-generation stage (the "front half").

Regenerates the coverage-curve snapshot the rest of the pipeline consumes, from
the vendored EMEA demand CSV. One pass per airport produces, into a dated
snapshot:

  routes_<date>/<AIRPORT>/routes.json         the coverage curve (solver input)
  routes_<date>/<AIRPORT>/demand_hexes.json   parent-hex polygons
  demand_lut_<date>/<AIRPORT>.json            trim-delta LUT (realized quality)
  dashboard_baseline/current.csv (regenerated)  per-airport today baseline

This is a faithful, flattened vendor of shared/scripts/coverage_curve_v5/run.py
(+ build_per_airport_outputs.py + the scale_to_emea current.csv emit + the LUT
build from build_demand_lut.py) with **no imports outside this asset**:
  - geo ranking / K_obs / snapshots  -> routegen.geo_greedy / routegen.bucket_alloc
  - route catalogue / dist buckets    -> routegen.greedy
  - centroids / boundaries            -> routegen.hex_geo (local h3)
  - baseline coverage                 -> routegen.coverage
  - constants (buckets, scenarios)    -> lib.curve_config (single source of truth)

**Airport-terminal hex exclusion — two sources (bug-compatible by default).**
The shipped 2026-05-12 `routes.json` snapshot was built by the original dashboard
route-gen, whose `AIRPORT_COORDS` table only carried coordinates for 15 hub
airports; for the other ~178 airports it silently skipped the airport-terminal
hex exclusion. The demand LUTs, by contrast, were built from the *complete*
`config/airport_coords.json`, so they excluded the terminal ring for all 193.

To reproduce the shipped snapshot **exactly** (Anton's "do not re-generate the
routes" → reproduce-as-is), this generator therefore uses TWO coord sources:
  - route / baseline / demand_hexes exclusion  -> config/airport_coords_routegen.json
    (the vendored 15-entry table, with its gaps — matches the published curves)
  - demand-LUT exclusion                        -> config/airport_coords.json
    (the complete 193-entry table — matches the published LUTs)
Set `full_exclusion=True` (config `regenerate_full_exclusion`) to apply the
complete table to routes too — the *corrected* refresh, which changes ~178
airports' curves and is opt-in only.

The default pipeline does NOT call this (it consumes the vendored snapshot). It
runs only when `regenerate_routes` is enabled — see scripts/cli.py Step 0.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import pandas as pd

from routegen.geo_greedy import apply_neighbour_separation, rank_hexes_by_geo
from routegen.bucket_alloc import (build_snapshots, per_hex_k_obs,
                                   _build_lookup_tables)
from routegen.coverage import compute_today_coverage
from routegen.hex_geo import cell_boundaries, resolve_centroids, snap_to_polygon
from routegen.observations import (load_airport_competitor_map,
                                   load_observations)
from lib.curve_config import BUCKET_ORDER, SCENARIOS, TIME_BUCKET_MID_HOUR

log = logging.getLogger("routegen.build_routes")

USER_EMAIL = "anton.tereshchuk@uber.com"
DEFAULT_HEX_SIZE = 7
DEFAULT_LOOKBACK_WEEKS = 4
N_TIME_BUCKETS = len(BUCKET_ORDER)

# Cap on commissioned routes so the curve doesn't run into the long geo tail.
# Concrete cap = min(ceiling, smallest N where geo_cum >= auto cap). Verbatim
# from shared coverage_curve_v5/run.py (ceiling 370 = Anton's v3 CDG budget).
DEFAULT_MAX_ROUTES_CEILING = 370
DEFAULT_AUTO_GEO_CAP = 0.95


# ---------------------------------------------------------------------------
# Per-airport helpers (vendored from coverage_curve_v5/run.py, coords injected)
# ---------------------------------------------------------------------------

def _airport_ring_hexes(airport: str, coords: dict | None, hex_size: int) -> set[str]:
    """Airport's parent hex + its 6 neighbours (k=1 grid disk) — excluded from
    optimisation. Mirror of run._airport_excluded_hexes / build_extraction_list.
    _airport_ring_hexes; coords are passed in (from config/airport_coords.json).
    """
    if not coords:
        log.warning("[%s] No coords entry — skipping hex exclusion", airport)
        return set()
    try:
        import h3  # type: ignore
    except Exception as exc:  # pragma: no cover
        log.warning("[%s] h3 not available — skipping hex exclusion: %s",
                    airport, exc)
        return set()
    lat, lng = float(coords["lat"]), float(coords["lng"])
    if hasattr(h3, "latlng_to_cell"):  # h3 v4
        parent = h3.latlng_to_cell(lat, lng, hex_size)
        ring = h3.grid_disk(parent, 1)
    else:  # h3 v3
        parent = h3.geo_to_h3(lat, lng, hex_size)  # type: ignore[attr-defined]
        ring = h3.k_ring(parent, 1)  # type: ignore[attr-defined]
    return {str(parent)} | {str(c) for c in ring}


def _coerce(df: pd.DataFrame, airport: str) -> pd.DataFrame:
    df = df.copy()
    for col in ("airport_code", "trip_type", "hex_id", "child_hex_id",
                "time_bucket", "dist_bucket"):
        if col in df.columns:
            df[col] = df[col].astype(str)
    if "sessions" in df.columns:
        df["sessions"] = pd.to_numeric(df["sessions"],
                                       errors="coerce").fillna(0).astype(int)
    if "avg_dist_km" in df.columns:
        df["avg_dist_km"] = pd.to_numeric(df["avg_dist_km"], errors="coerce")
    if "airport_code" in df.columns:
        df = df[df["airport_code"].str.upper() == airport].copy()
    return df


def _enrich_routes_with_centroids(catalogue: pd.DataFrame,
                                  user_email: str) -> dict[str, dict]:
    """Resolve dominant-child centroids and snap each to its parent boundary."""
    if catalogue.empty:
        return {}
    child_ids = set(catalogue["child_hex_id"].astype(str))
    parent_ids = set(catalogue["hex_id"].astype(str))
    centroids = resolve_centroids(child_ids, user_email=user_email)
    parent_boundaries = cell_boundaries(parent_ids, user_email=user_email)
    child_to_parent = dict(zip(catalogue["child_hex_id"].astype(str),
                               catalogue["hex_id"].astype(str)))
    for child_id, c in list(centroids.items()):
        boundary = parent_boundaries.get(child_to_parent.get(child_id))
        if not boundary:
            continue
        snapped = snap_to_polygon(c, boundary)
        if snapped != c:
            centroids[child_id] = snapped
    return {cid: {"lat": lat, "lng": lng} for cid, (lat, lng) in centroids.items()}


def _ordered_routes_json(catalogue: pd.DataFrame, centroids: dict[str, dict],
                         scenarios: tuple[tuple[str, float], ...]) -> list[dict]:
    out: list[dict] = []
    for r in catalogue.itertuples(index=False):
        row: dict = {
            "rank":                 int(r.rank),
            "hex_id":               str(r.hex_id),
            "child_hex_id":         str(r.child_hex_id),
            "centroid":             centroids.get(str(r.child_hex_id),
                                                  {"lat": None, "lng": None}),
            "dist_bucket":          str(r.dist_bucket),
            "avg_dist_km":          round(float(r.avg_dist_km), 3),
            "n_sessions_in_parent": int(r.sessions_hex),
            "n_sessions_in_child":  int(r.sessions_child),
            "geo_share":            round(float(r.geo_share), 6),
            "geo_cum":              round(float(r.geo_cum), 6),
        }
        for scen, _ in scenarios:
            row[f"k_obs_{scen}"] = int(getattr(r, f"k_obs_{scen}"))
            row[f"picked_buckets_{scen}"] = list(getattr(r, f"picked_buckets_{scen}"))
            row[f"sample_hours_{scen}"] = list(getattr(r, f"sample_hours_{scen}"))
        out.append(row)
    return out


def _resolve_max_routes(catalogue: pd.DataFrame, override: int) -> int:
    n_total = int(len(catalogue))
    if n_total == 0:
        return 0
    if override and override > 0:
        return min(override, n_total)
    auto_n = n_total
    for i, gc in enumerate(catalogue["geo_cum"].tolist(), start=1):
        if gc >= DEFAULT_AUTO_GEO_CAP - 1e-12:
            auto_n = i
            break
    return min(auto_n, DEFAULT_MAX_ROUTES_CEILING, n_total)


def _compute_baseline(cells_for_trip: pd.DataFrame, obs_for_trip: pd.DataFrame,
                      lookback_weeks: int = 4) -> dict:
    """Today's coverage on the exclusion-filtered demand+observations."""
    if cells_for_trip.empty:
        return {}
    cov = compute_today_coverage(
        cells_for_trip[["hex_id", "time_bucket", "dist_bucket", "sessions"]].copy(),
        obs_for_trip[["hex_id", "time_bucket", "dist_bucket", "num_observations"]].copy()
            if not obs_for_trip.empty else pd.DataFrame(
                columns=["hex_id", "time_bucket", "dist_bucket", "num_observations"]),
    )
    n_weeks = max(1, int(lookback_weeks))
    obs_total = int(cov.n_observations_4w)
    return {
        "n_today": int(cov.n_unique_routes_today),
        "obs_today_wk": int(round(obs_total / n_weeks)),
        "obs_today_total": obs_total,
        "obs_today_lookback_weeks": n_weeks,
        "sample": round(float(cov.sample_coverage_today), 4),
        "geo": round(float(cov.geo_coverage_today), 4),
        "domain": "v5_excluded",
    }


def _process_trip_type(airport: str, trip_type: str, cells_all: pd.DataFrame,
                       user_email: str, obs_all: pd.DataFrame | None = None) -> dict:
    cells = cells_all[cells_all["trip_type"] == trip_type].copy()
    if cells.empty:
        return {"n_max_routes": 0, "ordered_routes": [], "snapshots_by_scenario": {}}

    bucket_order = tuple(b for b in BUCKET_ORDER
                         if b in set(cells["time_bucket"].astype(str).unique()))
    if not bucket_order:
        bucket_order = BUCKET_ORDER

    # Step 1 — geo ranking + cap.
    catalogue = rank_hexes_by_geo(cells)
    n_total_routes = int(len(catalogue))
    max_routes = _resolve_max_routes(catalogue, 0)
    if max_routes < n_total_routes:
        catalogue = catalogue.head(max_routes).reset_index(drop=True)
    # Step 1b — neighbour separation (pin relocation).
    catalogue = apply_neighbour_separation(catalogue, cells)
    # Step 2 — per-scenario K_obs.
    thresholds = {scen: theta for scen, theta in SCENARIOS}
    catalogue = per_hex_k_obs(catalogue, cells, thresholds)
    # Centroids.
    centroids = _enrich_routes_with_centroids(catalogue, user_email)
    # Snapshots per scenario.
    snapshots_by_scenario: dict[str, list[dict]] = {}
    for scen, _ in SCENARIOS:
        snapshots_by_scenario[scen] = build_snapshots(catalogue, cells, scen,
                                                      bucket_order)
    # Baseline (today) on the same exclusion-filtered domain.
    obs_for_trip = (obs_all[obs_all["trip_type"] == trip_type]
                    if obs_all is not None and not obs_all.empty
                    else pd.DataFrame())
    baseline = _compute_baseline(cells, obs_for_trip,
                                 lookback_weeks=DEFAULT_LOOKBACK_WEEKS)
    return {
        "n_max_routes":          int(len(catalogue)),
        "n_total_routes":        int(n_total_routes),
        "total_sessions":        int(cells["sessions"].sum()),
        "bucket_order":          list(bucket_order),
        "ordered_routes":        _ordered_routes_json(catalogue, centroids, SCENARIOS),
        "snapshots_by_scenario": snapshots_by_scenario,
        "baseline":              baseline,
    }


def _write_routes(folder: Path, airport: str, per_trip: dict[str, dict],
                  today: date) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    bucket_order = next((p["bucket_order"] for p in per_trip.values()
                         if p.get("bucket_order")), list(BUCKET_ORDER))
    trip_block: dict[str, dict] = {}
    for trip, payload in per_trip.items():
        if not payload.get("ordered_routes"):
            continue
        trip_block[trip] = {
            "n_max_routes":   payload["n_max_routes"],
            "n_total_routes": payload.get("n_total_routes", payload["n_max_routes"]),
            "total_sessions": payload["total_sessions"],
            "ordered_routes": payload["ordered_routes"],
            "snapshots":      payload["snapshots_by_scenario"],
            "baseline":       payload.get("baseline", {}),
        }
    doc = {
        "algo_version":         "v5",
        "airport":              airport,
        "run_dt":               today.isoformat(),
        "n_time_buckets":       N_TIME_BUCKETS,
        "bucket_order":         bucket_order,
        "time_bucket_mid_hour": TIME_BUCKET_MID_HOUR,
        "scenarios":            {scen: {"threshold": theta}
                                 for scen, theta in SCENARIOS},
        "_implementation_guide": {
            "how_to_read": (
                "Pick a budget N (number of routes) and a scenario S in "
                "{max, mid, light}. Commission the first N entries from "
                "`trip_types[T].ordered_routes`. Each entry carries the "
                "parent hex_id, child hex_id and centroid, distance bucket, "
                "and per-scenario observation set: `k_obs_S`, "
                "`picked_buckets_S` (time-bucket labels), and "
                "`sample_hours_S` (middle hour of each bucket). The "
                "matching snapshot is `trip_types[T].snapshots[S][N-1]` "
                "(it carries n_observations, sample, geo, and per-bucket "
                "sb[10] / gb[10] arrays in `bucket_order`)."
            ),
            "two_steps": (
                "Step 1 (geo): hexes ranked by session-weighted geo coverage. "
                "Step 2 (sample): per-hex top-K time-buckets covering θ of "
                "that hex's sessions, where θ ∈ {1.0, 0.75, 0.5} for "
                "max/mid/light."
            ),
        },
        "trip_types":           trip_block,
    }
    (folder / "routes.json").write_text(json.dumps(doc, separators=(",", ":")))


def _write_demand_hexes(folder: Path, airport: str, cells: pd.DataFrame,
                        hex_size: int, user_email: str) -> None:
    if cells.empty:
        return
    folder.mkdir(parents=True, exist_ok=True)
    per_trip: dict[str, list[dict]] = {}
    all_hexes: set[str] = set()
    for trip_type, group in cells.groupby("trip_type"):
        totals = (group.groupby("hex_id", as_index=False)["sessions"].sum()
                       .sort_values("sessions", ascending=False))
        per_trip[str(trip_type)] = [
            {"hex_id": str(r["hex_id"]), "sessions": int(r["sessions"])}
            for _, r in totals.iterrows()
        ]
        all_hexes.update(r["hex_id"] for r in per_trip[str(trip_type)])
    boundaries = cell_boundaries(all_hexes, user_email=user_email)
    for rows in per_trip.values():
        for r in rows:
            r["boundary"] = [[round(lat, 6), round(lng, 6)]
                             for lat, lng in boundaries.get(r["hex_id"], [])]
    doc = {"airport": airport, "hex_size": hex_size, "trip_types": per_trip}
    (folder / "demand_hexes.json").write_text(json.dumps(doc))


def _lut_to_json(cells: pd.DataFrame) -> dict:
    """Per-trip demand LUT — exactly build_demand_lut.py's serialization."""
    sample_cell, denom_s, cell_hex_count, denom_g, total = _build_lookup_tables(cells)
    return {
        "sample_cell": {f"{t}|{d}": v for (t, d), v in sample_cell.items()},
        "denom_s": {t: v for t, v in denom_s.items()},
        "cell_hex_count": {f"{t}|{d}": v for (t, d), v in cell_hex_count.items()},
        "denom_g": {t: v for t, v in denom_g.items()},
        "total": total,
    }


# ---------------------------------------------------------------------------
# Batch orchestrator
# ---------------------------------------------------------------------------

def _read_airport_list(csv_path: Path) -> list[str]:
    import csv
    with csv_path.open() as f:
        return [r["airport_code"].strip().upper()
                for r in csv.DictReader(f) if r.get("airport_code", "").strip()]


def build(*, demand_csv: Path, obs_csv: Path, coords_path: Path,
          competitor_path: Path, out_routes_dir: Path, out_lut_dir: Path,
          current_csv_path: Path, route_coords_path: Path | None = None,
          full_exclusion: bool = False, airports: list[str] | None = None,
          hex_size: int = DEFAULT_HEX_SIZE, user_email: str = USER_EMAIL,
          run_dt: date | None = None) -> dict:
    """Regenerate routes.json + demand_hexes.json + demand LUT (+ current.csv)
    for `airports` (default: every airport in the demand CSV) from the vendored
    demand snapshot. Returns a small summary dict.

    Two coord sources drive the airport-terminal hex exclusion (see module
    docstring): `coords_path` (complete → LUTs) and `route_coords_path` (the
    vendored bug-compatible table → routes/baseline/demand_hexes). When
    `full_exclusion` is True, or `route_coords_path` is None, routes fall back to
    the complete `coords_path` — the corrected refresh.
    """
    today = run_dt or date.today()
    out_routes_dir.mkdir(parents=True, exist_ok=True)
    out_lut_dir.mkdir(parents=True, exist_ok=True)
    current_csv_path.parent.mkdir(parents=True, exist_ok=True)

    coords_map = json.loads(coords_path.read_text())           # complete -> LUTs
    if full_exclusion or route_coords_path is None:
        route_coords_map = coords_map                          # corrected refresh
        log.info("Route exclusion: FULL (complete coords) — corrected refresh")
    else:
        route_coords_map = json.loads(route_coords_path.read_text())
        log.info("Route exclusion: bug-compatible (%d-entry table) from %s",
                 len(route_coords_map), route_coords_path.name)
    competitor_map = load_airport_competitor_map(competitor_path)

    log.info("Loading demand from %s", demand_csv)
    demand = pd.read_csv(demand_csv)
    demand["airport_code"] = demand["airport_code"].astype(str).str.upper()
    demand_by_airport = {a: g for a, g in demand.groupby("airport_code")}
    log.info("demand: %d rows, %d airports", len(demand), len(demand_by_airport))

    if airports:
        want = [a.strip().upper() for a in airports if a.strip()]
    else:
        want = sorted(demand_by_airport)

    current_rows: list[dict] = []
    n_ok = n_skip = 0
    for i, airport in enumerate(want, 1):
        if airport not in demand_by_airport:
            log.warning("[%3d/%d] %s: no demand — skipping", i, len(want), airport)
            n_skip += 1
            continue
        cells_raw = _coerce(demand_by_airport[airport].copy(), airport)
        # Two exclusions: routes use the (bug-compatible) route coords, LUTs use
        # the complete coords — so both halves reproduce the shipped snapshot.
        route_excluded = _airport_ring_hexes(
            airport, route_coords_map.get(airport), hex_size)
        lut_excluded = _airport_ring_hexes(
            airport, coords_map.get(airport), hex_size)
        cells = (cells_raw[~cells_raw["hex_id"].astype(str).isin(route_excluded)].copy()
                 if route_excluded else cells_raw)
        cells_lut = (cells_raw[~cells_raw["hex_id"].astype(str).isin(lut_excluded)].copy()
                     if lut_excluded else cells_raw)
        obs_all = load_observations(obs_csv, airport, route_excluded, competitor_map)

        per_trip: dict[str, dict] = {}
        for trip in ("PU", "DO"):
            try:
                per_trip[trip] = _process_trip_type(airport, trip, cells,
                                                    user_email, obs_all=obs_all)
            except Exception as exc:  # keep the batch going
                log.error("[%3d/%d] %s/%s pipeline error: %s",
                          i, len(want), airport, trip, exc)
                per_trip[trip] = {"n_max_routes": 0, "ordered_routes": [],
                                  "snapshots_by_scenario": {}}

        if not any(p.get("ordered_routes") for p in per_trip.values()):
            log.warning("[%3d/%d] %s: no routes — skipping", i, len(want), airport)
            n_skip += 1
            continue

        _write_routes(out_routes_dir / airport, airport, per_trip, today)
        _write_demand_hexes(out_routes_dir / airport, airport, cells, hex_size,
                            user_email)

        # Demand LUT (per trip, on the complete-coords exclusion-filtered cells).
        lut: dict = {}
        for trip in ("PU", "DO"):
            tcells = cells_lut[cells_lut["trip_type"] == trip].copy()
            if not tcells.empty:
                lut[trip] = _lut_to_json(tcells)
        (out_lut_dir / f"{airport}.json").write_text(json.dumps(lut))

        # current.csv baseline row (from the per-trip baseline blocks).
        b_pu = per_trip.get("PU", {}).get("baseline", {}) or {}
        b_do = per_trip.get("DO", {}).get("baseline", {}) or {}
        current_rows.append({
            "airport_code": airport,
            "sample_coverage_today_PU": b_pu.get("sample", 0.0),
            "sample_coverage_today_DO": b_do.get("sample", 0.0),
            "geo_coverage_today_PU": b_pu.get("geo", 0.0),
            "geo_coverage_today_DO": b_do.get("geo", 0.0),
            "n_unique_routes_today_PU": float(b_pu.get("n_today", 0)),
            "n_unique_routes_today_DO": float(b_do.get("n_today", 0)),
            "_status": "",
        })
        n_ok += 1
        log.info("[%3d/%d] %s done", i, len(want), airport)

    cols = ["airport_code", "sample_coverage_today_PU", "sample_coverage_today_DO",
            "geo_coverage_today_PU", "geo_coverage_today_DO",
            "n_unique_routes_today_PU", "n_unique_routes_today_DO", "_status"]
    pd.DataFrame(current_rows, columns=cols).to_csv(current_csv_path, index=False)
    log.info("Regenerated %d airports (skipped %d) -> %s + %s + %s",
             n_ok, n_skip, out_routes_dir, out_lut_dir, current_csv_path)
    return {"ok": n_ok, "skipped": n_skip, "airports": want}
