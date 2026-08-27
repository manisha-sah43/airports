"""Canonical NPI coverage-curve constants — single source of truth for the asset.

Vendored verbatim from (fixes land in the source first, per Claude Code repo
rule #4):
  - BUCKET_ORDER, BUCKET_TO_DAYS, SHEET_COLUMNS, QUALITY_COLUMNS
        shared/scripts/coverage_curve_v5/build_extraction_list.py
  - TIME_BUCKET_MID_HOUR, SCENARIOS
        shared/scripts/coverage_curve_v4/config.py
  - TIER_RANK (best=high) — matches the ">= target" comparison in
        projects/2026-05-29_npi-coverage-curve-extraction/scripts/validate_against_input.py

These constants define the 10 time-occasion buckets, when each is collected,
and how (sample, geo) coverage maps to an NPI quality tier. Do not edit here
without updating the shared source.
"""

from __future__ import annotations

# The canonical 10-bucket order — mirrors the SQL emit order in
# current_observations_emea_bpo_mapped.sql (5 weekday + 5 weekend variants).
BUCKET_ORDER: tuple[str, ...] = (
    "wkd_night", "wkd_morning", "wkd_day", "wkd_evening", "wkd_late_evening",
    "wked_night", "wked_morning", "wked_day", "wrek_evening", "wrek_late_evening",
)

# 1=Mon..7=Sun. wkd_* buckets are collected Mon-Fri, wked_*/wrek_* on Sat-Sun.
BUCKET_TO_DAYS: dict[str, tuple[int, ...]] = {
    "wkd_night":         (1, 2, 3, 4, 5),
    "wkd_morning":       (1, 2, 3, 4, 5),
    "wkd_day":           (1, 2, 3, 4, 5),
    "wkd_evening":       (1, 2, 3, 4, 5),
    "wkd_late_evening":  (1, 2, 3, 4, 5),
    "wked_night":        (6, 7),
    "wked_morning":      (6, 7),
    "wked_day":          (6, 7),
    "wrek_evening":      (6, 7),
    "wrek_late_evening": (6, 7),
}

# Middle hour (local) of each bucket's window — tells the BPO when to collect.
# The `wrek_` prefix on the two weekend-evening buckets is a preserved SQL typo,
# kept verbatim so joins line up.
TIME_BUCKET_MID_HOUR: dict[str, int] = {
    "wkd_night":         3,
    "wkd_morning":       9,
    "wkd_day":          14,
    "wkd_evening":      19,
    "wkd_late_evening": 22,
    "wked_night":        3,
    "wked_morning":      9,
    "wked_day":         14,
    "wrek_evening":     19,
    "wrek_late_evening": 22,
}

# Scenario θ = fraction of a hex's sessions the picked time-buckets must cover.
#   max=1.00 (all occasions), mid=0.75, light=0.50.
SCENARIOS: tuple[tuple[str, float], ...] = (
    ("max",   1.00),
    ("mid",   0.75),
    ("light", 0.50),
)
VALID_SCENARIOS: frozenset[str] = frozenset(s for s, _ in SCENARIOS)

# Quality tiers, best -> worst. TIER_RANK higher == better, so "realized >=
# target" is a simple >= on the rank (matches validate_against_input.py).
QUALITY_TIERS_BEST_FIRST: tuple[str, ...] = ("Good", "Moderate", "Poor", "Unacceptable")
TIER_RANK: dict[str, int] = {"Unacceptable": 1, "Poor": 2, "Moderate": 3, "Good": 4}

# Columns of the flat PU_/DO_extraction sheet BEFORE enrichment (13 cols).
SHEET_COLUMNS: tuple[str, ...] = (
    "city_id", "dayofweek", "hourofday",
    "pickup_address", "dropoff_address",
    "pickup_lat", "pickup_lng", "dropoff_lat", "dropoff_lng",
    "pu_airport_code", "do_airport_code",
    "time_bucket", "route_rank",
)

# Columns of the per-cut quality_summary sheet (18 cols).
QUALITY_COLUMNS: tuple[str, ...] = (
    "airport_code", "trip_type",
    "current_sample_coverage", "current_g_coverage", "npi_quality",
    "to_be_wo_exclusion_sample_coverage", "to_be_wo_exclusion_g_coverage",
    "to_be_wo_exclusion_time_NPI_quality", "to_be_wo_exclusion_NPI_quality",
    "to_be_with_exclusion_sample_coverage", "to_be_with_exclusion_g_coverage",
    "to_be_with_exclusion_time_NPI_quality", "to_be_with_exclusion_NPI_quality",
    "n_obs_current", "n_routes_current",
    "n_obs_to_be_added", "n_extraction_rows", "n_routes_to_be_added",
)
