"""Snapshot -> NPI quality tiers. The load-bearing quality logic of the asset.

`_quality_from_snapshot` is vendored verbatim from
shared/scripts/coverage_curve_v5/build_extraction_list.py. It implements the
score-rounded tiering that the v5 dashboard, the v2 master sheet `curves` tab,
and Anton's `output_scenario` targets all use.

⚠️ time_NPI_quality = HALF-UP round of the MEAN per-bucket score (empty buckets
count as Unacceptable=2.0), NOT the worst-tier-present ("headline") aggregation.
Using the headline tier reports "Unacceptable" for a cut where 8/10 buckets are
Good. This mistake bit the source project twice — do not reintroduce it.
"""

from __future__ import annotations

import math

from .quality_lookup import classify
from .curve_config import BUCKET_ORDER


def snapshot_at(snapshots: list[dict], n_routes: int) -> dict | None:
    """Return the snapshot at exactly n_routes, or the last one <= n_routes."""
    if not snapshots:
        return None
    best = None
    for s in snapshots:
        if s["n_routes"] <= n_routes:
            best = s
    return best or snapshots[0]


def quality_from_snapshot(snap: dict | None) -> tuple[float, float, str, str]:
    """Return (sample, geo, NPI_quality_overall, time_NPI_quality).

    NPI_quality_overall = classify(sample, geo) on the scalar coverages.
    time_NPI_quality    = tier of HALF-UP round(mean per-bucket score over the
                          10 occasions; missing buckets = Unacceptable=2.0).
    """
    if not snap:
        return 0.0, 0.0, "Unacceptable", "Unacceptable"
    s = float(snap.get("sample", 0.0))
    g = float(snap.get("geo", 0.0))
    overall = classify(s, g).quality
    sb = snap.get("sb") or []
    gb = snap.get("gb") or []
    if not sb:
        return s, g, overall, "Unacceptable"
    score_sum = 0.0
    n = 0
    for i in range(min(len(sb), len(gb), len(BUCKET_ORDER))):
        score_sum += classify(float(sb[i]), float(gb[i])).score
        n += 1
    if n == 0:
        return s, g, overall, "Unacceptable"
    mean_score = score_sum / n
    # HALF-UP rounding (Google Sheets convention); Python's round() is banker's
    # rounding and would map 2.5 -> 2 instead of 3.
    rounded = int(math.floor(mean_score + 0.5))
    tier_by_score = {5: "Good", 4: "Moderate", 3: "Poor", 2: "Unacceptable"}
    time_tier = tier_by_score.get(rounded, "Unacceptable" if rounded < 2 else "Good")
    return s, g, overall, time_tier
