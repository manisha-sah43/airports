"""Per-time-bucket NPI quality classification (Anton's spec, 2026-05-11).

The 16-row table maps (sample_coverage, geo_coverage) → (Quality, Score).
Breakpoints from the table:
  sCoverage ∈ [0, 55, 75, 90, 100]  → Unacceptable, Poor, Moderate, Good
  gCoverage ∈ [0, 40, 60, 70, 100]  → Unacceptable, Poor, Moderate, Good

Boundary convention matches the table verbatim: each row is `Min ≤ x ≤ Max`,
i.e. inclusive on both ends; the upper bound of one band == the lower bound
of the next (e.g. exactly 0.55 is Poor's upper edge AND Moderate's lower
edge). For tied boundaries we pick the *higher* tier to match the user's
"90%" / "75%" / "55%" thresholds being aspirational targets.

Used both at run time (Python, baked into snapshots) and in the dashboard
JS (constant copy of the same table) so the labels never disagree.
"""

from __future__ import annotations

from dataclasses import dataclass

# (min_s, max_s, min_g, max_g, quality_label, score)
# Order: descending sample band, then descending geo band. Matches the
# screenshot Anton sent.
_RULES: tuple[tuple[float, float, float, float, str, float], ...] = (
    (0.90, 1.00, 0.70, 1.00, "Good",         5.0),
    (0.90, 1.00, 0.60, 0.70, "Good",         5.0),
    (0.90, 1.00, 0.40, 0.60, "Poor",         3.0),
    (0.90, 1.00, 0.00, 0.40, "Unacceptable", 2.0),
    (0.75, 0.90, 0.70, 1.00, "Good",         5.0),
    (0.75, 0.90, 0.60, 0.70, "Moderate",     4.0),
    (0.75, 0.90, 0.40, 0.60, "Poor",         3.0),
    (0.75, 0.90, 0.00, 0.40, "Unacceptable", 2.0),
    (0.55, 0.75, 0.70, 1.00, "Poor",         3.0),
    (0.55, 0.75, 0.60, 0.70, "Poor",         3.0),
    (0.55, 0.75, 0.40, 0.60, "Poor",         3.0),
    (0.55, 0.75, 0.00, 0.40, "Unacceptable", 2.0),
    (0.00, 0.55, 0.70, 1.00, "Unacceptable", 2.0),
    (0.00, 0.55, 0.60, 0.70, "Unacceptable", 2.0),
    (0.00, 0.55, 0.40, 0.60, "Unacceptable", 2.0),
    (0.00, 0.55, 0.00, 0.40, "Unacceptable", 2.0),
)

QUALITY_TIERS: tuple[str, ...] = ("Good", "Moderate", "Poor", "Unacceptable")
TIER_RANK: dict[str, int] = {q: i for i, q in enumerate(QUALITY_TIERS)}


@dataclass(frozen=True)
class Quality:
    quality: str
    score: float


def classify(s_cov: float, g_cov: float) -> Quality:
    """Look up the (Quality, Score) for a (sample_coverage, geo_coverage) pair.

    Both inputs are clipped to [0, 1]. Boundary points fall into the higher
    tier (90% → Good, not Moderate; 70% gCov → Good, not Moderate; etc.).
    """
    s = max(0.0, min(1.0, float(s_cov)))
    g = max(0.0, min(1.0, float(g_cov)))
    # Walk the table top-down (highest sample band first). The first rule
    # whose s ∈ [min_s, max_s] AND g ∈ [min_g, max_g] wins.
    for min_s, max_s, min_g, max_g, label, score in _RULES:
        s_in = (s >= min_s - 1e-12) and (s <= max_s + 1e-12)
        g_in = (g >= min_g - 1e-12) and (g <= max_g + 1e-12)
        # Tie-break for boundary == upper edge of one row and lower edge
        # of the next: pick the higher tier (the row with the higher min_s
        # / min_g). Because we walk top-down by descending sample band, the
        # first match is already the higher-sample tier; for geo we sort
        # within-block highest first by construction of _RULES.
        if s_in and g_in:
            return Quality(quality=label, score=score)
    # Defensive fallback (shouldn't happen for clipped inputs).
    return Quality(quality="Unacceptable", score=2.0)


def summarise(buckets: dict[str, dict]) -> dict:
    """Aggregate per-bucket {sample, geo, quality, score} into a summary.

    ⚠️ DO NOT USE `headline_tier` AS THE PUBLIC time_NPI_quality VALUE. ⚠️
    The v2 master sheet `curves` tab, the v5 dashboard, and Anton's
    `output_scenario` tab all display `round(mean_score) → tier`, NOT
    `headline_tier` (worst-tier-present). Using headline_tier reports
    "Unacceptable" for a cut where 8/10 buckets are Good — which does not
    match planning targets. Canonical implementation:
    `shared/scripts/coverage_curve_v5/build_extraction_list.py:_quality_from_snapshot`.
    Bit twice (last 2026-05-29). See memory time_npi_quality_aggregation.md.

    Returns:
      {
        "counts":          {Good:N, Moderate:N, Poor:N, Unacceptable:N},
        "weighted_score":  float (mean of `score` across non-empty buckets),
        "headline_tier":   the worst non-empty tier — DIAGNOSTIC ONLY,
                           NOT FOR DISPLAY.
      }
    """
    counts = {q: 0 for q in QUALITY_TIERS}
    scores: list[float] = []
    for _, row in buckets.items():
        q = row.get("quality") or "Unacceptable"
        counts[q] = counts.get(q, 0) + 1
        scores.append(float(row.get("score", 2.0)))
    if not scores:
        return {"counts": counts, "weighted_score": 0.0,
                "headline_tier": "Unacceptable"}
    headline = next((q for q in ("Unacceptable", "Poor", "Moderate", "Good")
                     if counts[q] > 0), "Unacceptable")
    return {
        "counts": counts,
        "weighted_score": round(sum(scores) / len(scores), 3),
        "headline_tier": headline,
    }
