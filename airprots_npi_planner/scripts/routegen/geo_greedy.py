"""Step 1: rank parent hexes by session-weighted geo coverage.

The geo objective is additive and disjoint over hexes — `geo(N) = sum of top-N
geo_share` — so the greedy is **exactly** the sort by geo_share desc. No
time-bucket logic enters this step. The output is the order in which routes
should be commissioned to maximise geo coverage at any N.

Each parent hex contributes one route, pinned at its dominant child cell
(reused unchanged from v1 via `build_route_catalogue`).

Neighbour-separation: after the rank is fixed, walk the catalogue top-down
and re-pick each parent's pin so its child cell does NOT touch any
already-pinned child cell at child resolution. Falls back to the dominant
child if no non-conflicting child exists in the parent.
"""

from __future__ import annotations

import logging

import pandas as pd

# Vendored into the asset: flattened import (was
# `from ..coverage_curve.greedy import ...` in shared/scripts/coverage_curve_v5).
from routegen.greedy import (_bucket_of_km, _hex_avg_dist_km,
                             build_route_catalogue)

log = logging.getLogger("routegen.geo_greedy")


def rank_hexes_by_geo(cells: pd.DataFrame) -> pd.DataFrame:
    """Return one route per parent hex, ordered by greedy geo coverage.

    Inputs: long-form `cells` for ONE (airport, trip_type) — same shape as
    v1/v4 (hex_id, child_hex_id, time_bucket, dist_bucket, sessions,
    avg_dist_km).

    Output columns:
      hex_id, child_hex_id, dist_bucket, avg_dist_km,
      sessions_hex, sessions_child, geo_share, geo_cum, rank
    Sorted: geo_share desc, hex_id asc (deterministic tie-break).
    `rank` is 1-based pick order; `geo_cum` is cumulative geo coverage AFTER
    this hex is commissioned.
    """
    cat = build_route_catalogue(cells)
    if cat.empty:
        out = cat.copy()
        for col in ("geo_share", "geo_cum", "rank"):
            out[col] = pd.Series(dtype="float64" if col != "rank" else "int64")
        return out

    total_sessions = float(cells["sessions"].sum())
    if total_sessions <= 0:
        out = cat.copy()
        out["geo_share"] = 0.0
        out["geo_cum"] = 0.0
        out["rank"] = range(1, len(out) + 1)
        return out

    cat = cat.copy()
    cat["sessions_hex"] = cat["sessions_hex"].astype(float)
    cat["geo_share"] = cat["sessions_hex"] / total_sessions
    cat = (cat.sort_values(by=["sessions_hex", "hex_id"],
                            ascending=[False, True])
              .reset_index(drop=True))
    cat["geo_cum"] = cat["geo_share"].cumsum().clip(upper=1.0)
    cat["rank"] = range(1, len(cat) + 1)
    return cat


def _child_neighbours(child_id: str) -> set[str]:
    """1-ring of a child cell INCLUDING itself, as a set of strings.
    Returns just {child_id} if h3 is unavailable."""
    try:
        import h3                                                     # type: ignore
    except Exception:                                                 # pragma: no cover
        return {child_id}
    if hasattr(h3, "grid_disk"):                                       # h3 v4
        return {str(c) for c in h3.grid_disk(child_id, 1)}
    return {str(c) for c in h3.k_ring(child_id, 1)}                    # type: ignore[attr-defined]


def apply_neighbour_separation(catalogue: pd.DataFrame,
                               cells: pd.DataFrame) -> pd.DataFrame:
    """Re-pick the pin (child_hex_id) for each parent in rank order so that
    no two pinned children touch (1-ring) at child resolution.

    Walks `catalogue` top-down. For each parent:
      - List its children by sessions desc (tie: child_hex_id asc).
      - Pick the first child whose own cell + 6 neighbours are all clear
        of already-pinned children.
      - If no child is conflict-free, keep the original dominant child
        (we still commission the parent — we never drop a high-rank parent
        just because it's surrounded by earlier picks). Logged.
    Updates `child_hex_id`, `sessions_child`, `avg_dist_km`, `dist_bucket`
    in-place on a copy.

    Parent rank, geo_share, K_obs (parent-level) are untouched. Only the
    pin location and its derived dist_bucket can change.
    """
    if catalogue.empty or "child_hex_id" not in cells.columns:
        return catalogue.copy()

    cells = cells.copy()
    cells["hex_id"] = cells["hex_id"].astype(str)
    cells["child_hex_id"] = cells["child_hex_id"].astype(str)

    # Per-parent: child_hex_id sorted by sessions desc, with each child's
    # subgroup so we can recompute avg_dist_km if a non-dominant child wins.
    children_by_parent: dict[str, list[tuple[str, float, pd.DataFrame]]] = {}
    for parent, grp in cells.groupby("hex_id"):
        totals = (grp.groupby("child_hex_id", as_index=False)["sessions"]
                     .sum()
                     .sort_values(by=["sessions", "child_hex_id"],
                                  ascending=[False, True]))
        ordered = []
        for _, row in totals.iterrows():
            cid = str(row["child_hex_id"])
            sess = float(row["sessions"])
            sub = grp[grp["child_hex_id"] == cid]
            ordered.append((cid, sess, sub))
        children_by_parent[str(parent)] = ordered

    out = catalogue.copy()
    placed: set[str] = set()        # child cells already pinned
    blocked: set[str] = set()       # placed ∪ all their 1-ring neighbours
    n_relocated = 0
    n_fallback = 0

    for idx, row in out.iterrows():
        parent = str(row["hex_id"])
        original_child = str(row["child_hex_id"])
        candidates = children_by_parent.get(parent, [])
        chosen_cid: str | None = None
        chosen_sub: pd.DataFrame | None = None
        chosen_sess: float | None = None
        for cid, sess, sub in candidates:
            if cid in blocked:
                continue
            chosen_cid, chosen_sess, chosen_sub = cid, sess, sub
            break
        if chosen_cid is None:
            # All children of this parent are blocked — keep dominant child.
            chosen_cid = original_child
            for cid, sess, sub in candidates:
                if cid == chosen_cid:
                    chosen_sess, chosen_sub = sess, sub
                    break
            n_fallback += 1
        elif chosen_cid != original_child:
            n_relocated += 1

        # Update derived fields if pin changed.
        if chosen_cid != original_child and chosen_sub is not None:
            avg_km = _hex_avg_dist_km(chosen_sub)
            out.at[idx, "child_hex_id"]   = chosen_cid
            out.at[idx, "sessions_child"] = float(chosen_sess or 0.0)
            out.at[idx, "avg_dist_km"]    = avg_km
            out.at[idx, "dist_bucket"]    = _bucket_of_km(avg_km)

        # Mark the chosen pin's cell + 1-ring as blocked for later picks.
        ring = _child_neighbours(chosen_cid)
        placed.add(chosen_cid)
        blocked |= ring

    log.info("neighbour-separation: %d / %d pins relocated to a non-dominant child; "
             "%d parents had no conflict-free child (kept dominant)",
             n_relocated, len(out), n_fallback)
    return out
