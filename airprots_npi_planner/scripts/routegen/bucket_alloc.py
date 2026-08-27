"""Step 2: per-hex K_obs allocation under a scenario θ.

For each commissioned hex h_i and scenario θ ∈ {1.0, 0.75, 0.5}:
  K_obs(h_i, θ) = smallest top-K time-buckets (by sessions desc) whose
                  cumulative weight ≥ θ · sessions(h_i).
This is identical to v4's per-hex computation; we reuse the same primitive.

Sample / per-bucket coverage at the prefix of the first N routes uses the
v1-style cell definition (see `evaluate_prefix`), with the v4 fix for
per-bucket geo (count any hex h with sessions in t whose (t, d_h) is
covered, not just commissioned hexes — keeps geo[t] hitting 100% under
MAX saturation).
"""

from __future__ import annotations

import pandas as pd

# Vendored into the asset: flattened imports (were `..coverage_curve.greedy`
# and `..coverage_curve_v4.*` in shared/scripts/coverage_curve_v5). Constants now
# come from the asset's single-source-of-truth lib/curve_config.py.
from routegen.greedy import build_route_catalogue, _pick_top_k_for_threshold
from lib.curve_config import TIME_BUCKET_MID_HOUR


def _per_hex_buckets(cells: pd.DataFrame) -> dict[str, list[tuple[str, float]]]:
    """For each parent hex, return time-buckets sorted by sessions desc."""
    if cells.empty:
        return {}
    agg = (cells.groupby(["hex_id", "time_bucket"], as_index=False)["sessions"]
                .sum())
    out: dict[str, list[tuple[str, float]]] = {}
    for hex_id, grp in agg.groupby("hex_id"):
        items = [(str(r["time_bucket"]), float(r["sessions"]))
                 for _, r in grp.iterrows() if float(r["sessions"]) > 0]
        items.sort(key=lambda kv: (-kv[1], kv[0]))
        out[str(hex_id)] = items
    return out


def per_hex_k_obs(catalogue: pd.DataFrame, cells: pd.DataFrame,
                  thresholds: dict[str, float]) -> pd.DataFrame:
    """Add per-scenario `k_obs_<scen>`, `picked_buckets_<scen>`,
    `sample_hours_<scen>` columns to a Step-1 catalogue.

    `catalogue` is the output of `geo_greedy.rank_hexes_by_geo`.
    `thresholds` maps scenario label → θ (e.g. {"max": 1.0, "mid": 0.75}).
    """
    if catalogue.empty:
        return catalogue.copy()
    per_hex = _per_hex_buckets(cells)
    cat = catalogue.copy()
    sorted_buckets = cat["hex_id"].map(per_hex).apply(
        lambda x: x if isinstance(x, list) else [])
    for scenario, theta in thresholds.items():
        picks = sorted_buckets.apply(
            lambda b, t=theta: _pick_top_k_for_threshold(b, t))
        cat[f"picked_buckets_{scenario}"] = picks
        cat[f"k_obs_{scenario}"] = picks.apply(len)
        cat[f"sample_hours_{scenario}"] = picks.apply(
            lambda ps: [TIME_BUCKET_MID_HOUR.get(p, -1) for p in ps])
    return cat


def _build_lookup_tables(cells: pd.DataFrame
                         ) -> tuple[dict, dict, dict, dict, float]:
    """Pre-compute everything `evaluate_prefix` needs from `cells` (one trip).

    Returns:
      sample_cell  : (t, d) -> sessions(t, d)              [absolute sessions]
      denom_sample : t      -> sessions(t, *)              [absolute sessions]
      cell_hex_count: (t, d) -> # hexes h with d_h=d AND sessions(h, t)>0
      denom_geo_t  : t -> # hexes h with sessions(h, t) > 0
      total_sessions
    """
    cells = cells.copy()
    cells["hex_id"] = cells["hex_id"].astype(str)
    cells["time_bucket"] = cells["time_bucket"].astype(str)
    cells["dist_bucket"] = cells["dist_bucket"].astype(str)
    total_sessions = float(cells["sessions"].sum())

    td = (cells.groupby(["time_bucket", "dist_bucket"], as_index=False)
                ["sessions"].sum())
    sample_cell = {
        (r["time_bucket"], r["dist_bucket"]): float(r["sessions"])
        for _, r in td.iterrows()
    }
    denom_sample_t = (td.groupby("time_bucket")["sessions"].sum().to_dict())

    cat_all = build_route_catalogue(cells)
    hex_to_dist = dict(zip(cat_all["hex_id"].astype(str),
                            cat_all["dist_bucket"].astype(str)))

    ht = (cells.groupby(["hex_id", "time_bucket"], as_index=False)
                ["sessions"].sum())
    ht = ht[ht["sessions"] > 0]
    ht["dist_bucket"] = ht["hex_id"].map(hex_to_dist)
    cell_hex_count: dict[tuple[str, str], int] = {}
    denom_geo_t: dict[str, int] = {}
    for tb, grp in ht.groupby("time_bucket"):
        denom_geo_t[tb] = int(grp["hex_id"].nunique())
        for dd, sub in grp.groupby("dist_bucket"):
            cell_hex_count[(tb, dd)] = int(sub["hex_id"].nunique())
    return sample_cell, denom_sample_t, cell_hex_count, denom_geo_t, total_sessions


def build_snapshots(catalogue: pd.DataFrame, cells: pd.DataFrame,
                    scenario: str, bucket_order: tuple[str, ...]
                    ) -> list[dict]:
    """Compute one snapshot per N = 1..len(catalogue) for `scenario`.

    Each snapshot = {n_routes, n_observations, sample, geo, sb[10], gb[10]}.
    `sample`, `geo` are scalar v1-style coverages; `sb`/`gb` are per-bucket.
    """
    if catalogue.empty:
        return []
    sample_cell, denom_sample_t, cell_hex_count, denom_geo_t, total_sessions = (
        _build_lookup_tables(cells))
    if total_sessions <= 0:
        return []

    sample_w = {k: v / total_sessions for k, v in sample_cell.items()}

    covered_cells: set[tuple[str, str]] = set()
    cov_sample_per_t = {tb: 0.0 for tb in bucket_order}
    cov_geo_per_t = {tb: 0 for tb in bucket_order}
    sample_cov = 0.0
    geo_cov = 0.0
    cum_obs = 0

    snapshots: list[dict] = []
    pbcol = f"picked_buckets_{scenario}"
    kcol = f"k_obs_{scenario}"
    for i, r in enumerate(catalogue.itertuples(index=False), start=1):
        d_h = str(getattr(r, "dist_bucket"))
        picks = list(getattr(r, pbcol) or [])
        k_obs = int(getattr(r, kcol) or 0)
        cum_obs += k_obs
        # Geo: every commissioned hex contributes its geo_share once.
        geo_cov = min(geo_cov + float(getattr(r, "geo_share")), 1.0)
        # Sample: covered (t, d) cells from this hex's picks at d_h.
        for tb in picks:
            cell = (tb, d_h)
            if cell in covered_cells:
                continue
            covered_cells.add(cell)
            sample_cov = min(sample_cov + sample_w.get(cell, 0.0), 1.0)
            sess = sample_cell.get(cell, 0.0)
            if tb in cov_sample_per_t:
                cov_sample_per_t[tb] += sess
            if tb in cov_geo_per_t:
                cov_geo_per_t[tb] += cell_hex_count.get(cell, 0)

        sb, gb = [], []
        for tb in bucket_order:
            ds = denom_sample_t.get(tb, 0.0)
            dg = denom_geo_t.get(tb, 0)
            sb.append(round((cov_sample_per_t[tb] / ds) if ds > 0 else 0.0, 4))
            gb.append(round((cov_geo_per_t[tb] / dg) if dg > 0 else 0.0, 4))

        snapshots.append({
            "n_routes":       i,
            "n_observations": cum_obs,
            "sample":         round(sample_cov, 4),
            "geo":            round(geo_cov, 4),
            "sb":             sb,
            "gb":             gb,
        })
    return snapshots
