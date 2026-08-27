"""Weighted-sum greedy for joint sample/geo coverage maximisation.

Inputs (all per (airport, trip_type)):
- A long-form `cells` DataFrame with columns:
    hex_id, child_hex_id, time_bucket, dist_bucket, sessions, avg_dist_km
  (`hex_id` is the parent H3 cell at `hex_size`; `child_hex_id` is one
   resolution finer — see the SQL header. v1 inputs without `child_hex_id`
   are accepted: the inner step then degrades to v1 behaviour, treating the
   parent hex itself as the only child.)
- A weight `w` in [0, 1] for the objective:
    score = w * Δsample_cov + (1 - w) * Δgeo_cov

Outputs:
- selections: list[dict] of length N_max, in pick order, each row =
    {hex_id, child_hex_id, dist_bucket, delta_sample, delta_geo,
     sample_cov_after, geo_cov_after}
- N_max: first index at which both sample_cov and geo_cov reach `stop_at`

Two-step framing (v2, 2026-05-07):
- **Inner step** — for each parent `hex_id`, pick the dominant `child_hex_id`
  (the child with the most sessions; tie-break: smallest child_hex_id),
  compute its session-weighted average distance, and map that to a
  `dist_bucket`. The route from that parent will be pinned at the dominant
  child's centroid, and its `dist_bucket` reflects only the child's sessions.
  Done once, in `build_route_catalogue`.
- **Outer step** — the greedy below selects which parent hexes to commission.
  Geo coverage is keyed at the parent. Sample coverage is keyed at
  (time_bucket, dist_bucket) with the dist_bucket coming from step 1.
- Adding route r = (h_parent, d_h) covers every (t, d_h) with sample_w > 0
  (its observations span all 10 time_buckets) and covers parent hex h_parent.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

# Order distance buckets so 'smaller' < 'bigger' for tie-break.
_DIST_ORDER = (
    "0-5 km", "5-10 km", "10-20 km", "20-30 km", "30-40 km", "40-50 km",
    "50-60 km", "60-70 km", "70-80 km", "80-90 km", "90-100 km", "100+ km",
)
_DIST_RANK = {d: i for i, d in enumerate(_DIST_ORDER)}

# Bucket-edge mapping: byte-identical to bpo_npi_hourly_fixed.sql:142-155 / the
# new airport_session_demand.sql. Used to convert a hex's session-weighted
# average distance (km) back into a single dist_bucket label.
_DIST_EDGES_KM = (
    (5.0,   "0-5 km"),       # d <= 5
    (10.0,  "5-10 km"),      # 5 < d <= 10
    (20.0,  "10-20 km"),
    (30.0,  "20-30 km"),
    (40.0,  "30-40 km"),
    (50.0,  "40-50 km"),
    (60.0,  "50-60 km"),
    (70.0,  "60-70 km"),
    (80.0,  "70-80 km"),
    (90.0,  "80-90 km"),
    (100.0, "90-100 km"),
)
# d > 100 -> "100+ km" (handled below)


def _bucket_of_km(km: float) -> str:
    if km <= 0:
        return "0-5 km"
    for upper, label in _DIST_EDGES_KM:
        if km <= upper:
            return label
    return "100+ km"


@dataclass(frozen=True)
class GreedyResult:
    selections: list[dict]                  # ordered pick list, length N_max
    n_max: int                              # first step where both cov = 1.0
    final_sample_cov: float
    final_geo_cov: float
    n_unique_hexes: int
    n_unique_dist_buckets: int


def _hex_avg_dist_km(group: pd.DataFrame) -> float:
    """Session-weighted average distance for one hex.

    If `avg_dist_km` is present in `group` (real per-cell mean from SQL), use it
    weighted by `sessions`. Otherwise fall back to bucket midpoints — keeps the
    code working on older CSVs that pre-date the SQL change.
    """
    if "avg_dist_km" in group.columns and group["avg_dist_km"].notna().any():
        s = pd.to_numeric(group["sessions"], errors="coerce").fillna(0).astype(float)
        d = pd.to_numeric(group["avg_dist_km"], errors="coerce")
        # Drop rows where avg_dist_km is missing.
        mask = d.notna() & (s > 0)
        if mask.any():
            return float((s[mask] * d[mask]).sum() / s[mask].sum())
    # Midpoint fallback.
    midpoints = {
        "0-5 km": 2.5, "5-10 km": 7.5, "10-20 km": 15.0, "20-30 km": 25.0,
        "30-40 km": 35.0, "40-50 km": 45.0, "50-60 km": 55.0, "60-70 km": 65.0,
        "70-80 km": 75.0, "80-90 km": 85.0, "90-100 km": 95.0, "100+ km": 110.0,
    }
    s = pd.to_numeric(group["sessions"], errors="coerce").fillna(0).astype(float)
    m = group["dist_bucket"].map(midpoints).astype(float)
    return float((s * m).sum() / s.sum()) if s.sum() > 0 else 0.0


def build_route_catalogue(cells: pd.DataFrame) -> pd.DataFrame:
    """Inner step (v2): one row per parent hex_id, pinned at the dominant child.

    For each parent `hex_id`:
      1. Pick the dominant `child_hex_id` — the child with the most sessions.
         Tie-break: smallest child_hex_id (deterministic).
      2. Compute that child's session-weighted average distance.
      3. Map the child average to its `dist_bucket`.
      4. Record parent and child session totals so the analyst surface can
         see how concentrated demand is inside the parent.

    Output columns:
      hex_id (parent), child_hex_id, dist_bucket, avg_dist_km,
      sessions_hex (parent total), sessions_child (dominant child total)

    Backwards compatible: if `child_hex_id` is missing from `cells`
    (v1 inputs), the function degrades to v1 behaviour by treating the
    parent hex itself as the only child — the catalogue still has the same
    shape and the dist_bucket then reflects the whole-parent average.
    """
    cols_out = ["hex_id", "child_hex_id", "dist_bucket", "avg_dist_km",
                "sessions_hex", "sessions_child"]
    if cells.empty:
        return pd.DataFrame(columns=cols_out)
    cells = cells.copy()
    if "child_hex_id" not in cells.columns:
        cells["child_hex_id"] = cells["hex_id"]
    cells["child_hex_id"] = cells["child_hex_id"].astype(str)
    rows: list[dict] = []
    for hex_id, group in cells.groupby("hex_id"):
        child_totals = (group.groupby("child_hex_id", as_index=False)["sessions"]
                             .sum()
                             .sort_values(by=["sessions", "child_hex_id"],
                                          ascending=[False, True]))
        if child_totals.empty:
            continue
        dominant_child = str(child_totals.iloc[0]["child_hex_id"])
        sessions_in_child = float(child_totals.iloc[0]["sessions"])
        sessions_in_parent = float(child_totals["sessions"].sum())
        child_subgroup = group[group["child_hex_id"] == dominant_child]
        avg_km = _hex_avg_dist_km(child_subgroup)
        rows.append({
            "hex_id": str(hex_id),
            "child_hex_id": dominant_child,
            "dist_bucket": _bucket_of_km(avg_km),
            "avg_dist_km": avg_km,
            "sessions_hex": sessions_in_parent,
            "sessions_child": sessions_in_child,
        })
    return pd.DataFrame(rows, columns=cols_out)


def weighted_sum_greedy(cells: pd.DataFrame, weight: float,
                        stop_at: float = 1.0) -> GreedyResult:
    """Run the weighted-sum greedy until both coverages hit ``stop_at`` (default 1.0).

    `stop_at` is the coverage threshold both metrics must reach to terminate.
    Set to 0.95 to halt the curve once we've covered 95% of both sample and
    geo demand — useful when the long tail of rare cells isn't actionable.
    """
    if not 0.0 <= weight <= 1.0:
        raise ValueError(f"weight must be in [0, 1]; got {weight}")
    if not 0.0 < stop_at <= 1.0:
        raise ValueError(f"stop_at must be in (0, 1]; got {stop_at}")
    if cells.empty:
        return GreedyResult([], 0, 0.0, 0.0, 0, 0)

    cells = cells.copy()
    cells["sessions"] = cells["sessions"].astype(float)
    total = float(cells["sessions"].sum())
    if total <= 0:
        return GreedyResult([], 0, 0.0, 0.0, 0, 0)

    # Sample weight per (time_bucket, dist_bucket): the population to cover.
    sample_w = cells.groupby(["time_bucket", "dist_bucket"], as_index=False)["sessions"].sum()
    sample_w["weight"] = sample_w["sessions"] / total
    sample_weights: dict[tuple[str, str], float] = {
        (str(r["time_bucket"]), str(r["dist_bucket"])): float(r["weight"])
        for _, r in sample_w.iterrows()
    }
    # Pre-index buckets by dist_bucket for O(1) lookup of "what (t, d) does
    # picking dist=d cover".
    buckets_by_dist: dict[str, list[tuple[tuple[str, str], float]]] = {}
    for (t, d), wgt in sample_weights.items():
        buckets_by_dist.setdefault(d, []).append(((t, d), wgt))

    # Route catalogue: one per hex.
    routes = build_route_catalogue(cells)
    routes["geo_weight"] = routes["sessions_hex"] / total
    # Pre-compute the per-route potential sample contribution, summed over t —
    # used as a deterministic rank, not the live Δsample.
    routes["dist_rank"] = routes["dist_bucket"].map(_DIST_RANK).fillna(len(_DIST_ORDER))
    routes = routes.sort_values(by=["geo_weight", "dist_rank", "hex_id"],
                                ascending=[False, True, True]).reset_index(drop=True)

    # Live state.
    covered_buckets: set[tuple[str, str]] = set()
    covered_hexes: set[str] = set()
    sample_cov = 0.0
    geo_cov = 0.0
    selections: list[dict] = []

    n_total_routes = len(routes)
    if n_total_routes == 0:
        return GreedyResult([], 0, 0.0, 0.0, 0, 0)

    remaining = list(range(n_total_routes))
    while remaining:
        # Score each remaining route under current state.
        best_idx: int | None = None
        best_score = -1.0
        best_dsample = 0.0
        best_dgeo = 0.0
        for ridx in remaining:
            r = routes.iloc[ridx]
            d_h = r["dist_bucket"]
            h = r["hex_id"]
            d_sample = 0.0
            for (t, d), wgt in buckets_by_dist.get(d_h, ()):
                if (t, d) not in covered_buckets and wgt > 0:
                    d_sample += wgt
            d_geo = float(r["geo_weight"]) if h not in covered_hexes else 0.0
            score = weight * d_sample + (1.0 - weight) * d_geo
            # Tie-break: greater geo_weight, then greater d_sample, then
            # earlier route index (already deterministically sorted).
            tie = (score, float(r["geo_weight"]), d_sample, -ridx)
            best_tie = (best_score, float(routes.iloc[best_idx]["geo_weight"]) if best_idx is not None else -1.0,
                        best_dsample, -best_idx if best_idx is not None else -10**9)
            if best_idx is None or tie > best_tie:
                best_idx = ridx
                best_score = score
                best_dsample = d_sample
                best_dgeo = d_geo

        chosen = routes.iloc[best_idx]
        d_h = chosen["dist_bucket"]
        h = chosen["hex_id"]
        # Apply.
        for (t, d), wgt in buckets_by_dist.get(d_h, ()):
            if (t, d) not in covered_buckets and wgt > 0:
                covered_buckets.add((t, d))
                sample_cov += wgt
        if h not in covered_hexes:
            covered_hexes.add(h)
            geo_cov += float(chosen["geo_weight"])

        selections.append({
            "hex_id": str(h),
            "child_hex_id": str(chosen["child_hex_id"]),
            "dist_bucket": str(d_h),
            "delta_sample": best_dsample,
            "delta_geo": best_dgeo,
            "sample_cov_after": min(sample_cov, 1.0),
            "geo_cov_after": min(geo_cov, 1.0),
        })
        remaining.remove(best_idx)

        # Early stop: both reached the threshold.
        if sample_cov >= stop_at - 1e-12 and geo_cov >= stop_at - 1e-12:
            break

    n_max = len(selections)
    return GreedyResult(
        selections=selections,
        n_max=n_max,
        final_sample_cov=min(sample_cov, 1.0),
        final_geo_cov=min(geo_cov, 1.0),
        n_unique_hexes=len(covered_hexes),
        n_unique_dist_buckets=len({d for _, d in covered_buckets}),
    )


def _pick_top_k_for_threshold(buckets: list[tuple[str, float]],
                              threshold: float) -> list[str]:
    """Smallest top-K time-buckets whose cumulative sessions cross threshold.

    Vendored verbatim from shared/scripts/coverage_curve_v4/greedy_v2.py so the
    K_obs allocation primitive lives alongside the route catalogue in one local
    module (was `from ..coverage_curve_v4.greedy_v2 import ...`).
    """
    if not buckets:
        return []
    total = sum(w for _, w in buckets)
    if total <= 0 or threshold <= 0:
        return [buckets[0][0]]
    target = total * threshold
    cum = 0.0
    picked: list[str] = []
    for label, w in buckets:
        picked.append(label)
        cum += w
        if cum >= target - 1e-12:
            break
    return picked
