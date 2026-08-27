"""Observation-loading helpers for route generation (baseline computation).

Adapted from shared/scripts/coverage_curve_v5/observations.py — the only change
is that the airport→competitor map and the observations CSV path are passed in
explicitly (the shared version hard-coded a `PROJECTS_DIR`-relative lookup). The
competitor-filter + hex-exclusion logic is byte-identical.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd

log = logging.getLogger("routegen.observations")


def load_airport_competitor_map(path: Path) -> dict[str, str]:
    """Load the airport→competitor mapping from a JSON file.

    Returns {airport_code: competitor_name}. Airports mapped to 'Other' have no
    canonical competitor — baseline is suppressed for them.
    """
    if not path.exists():
        log.warning("No airport→competitor map at %s — baseline filter disabled",
                    path)
        return {}
    return json.loads(path.read_text())


def load_observations(obs_csv: Path, airport: str,
                      excluded_hexes: set[str],
                      competitor_map: dict[str, str]) -> pd.DataFrame:
    """Load per-hex observations for `airport` and drop excluded hexes.

    The BPO-mapped CSV contains multiple rows per (airport, trip, hex, t, d) —
    one per competitor. We filter to the airport's canonical competitor via the
    passed-in `competitor_map` (CDG → Bolt, MAD → Cabify, etc.). Airports mapped
    to 'Other' return an empty frame (no comparable competitor → no baseline).
    """
    if obs_csv is None or not obs_csv.exists():
        log.warning("[%s] No observations CSV — baseline will not be embedded "
                    "in routes.json", airport)
        return pd.DataFrame()
    log.info("[%s] Loading observations from %s", airport, obs_csv.name)
    df = pd.read_csv(obs_csv)
    for col in ("airport_code", "trip_type", "hex_id", "time_bucket",
                "dist_bucket", "competitor"):
        if col in df.columns:
            df[col] = df[col].astype(str)
    if "num_observations" in df.columns:
        df["num_observations"] = pd.to_numeric(df["num_observations"],
                                                errors="coerce").fillna(0).astype(int)
    df = df[df["airport_code"].str.upper() == airport].copy()
    # Filter to the canonical competitor for this airport. Only enforce when the
    # obs CSV actually carries a `competitor` column.
    if "competitor" in df.columns and not df.empty:
        target_comp = competitor_map.get(airport)
        if not target_comp:
            log.warning("[%s] No airport→competitor mapping entry — keeping "
                        "all competitors (baseline may double-count)", airport)
        elif target_comp.lower() == "other":
            log.warning("[%s] mapped to '%s' (no canonical competitor) — "
                        "suppressing baseline", airport, target_comp)
            return pd.DataFrame()
        else:
            present = sorted(df["competitor"].unique())
            df = df[df["competitor"] == target_comp].copy()
            log.info("[%s] competitor filter: target=%s, kept %d rows; "
                     "competitors present in raw obs: %s",
                     airport, target_comp, len(df), present)
            if df.empty:
                log.warning("[%s] no obs match competitor=%s — suppressing baseline",
                            airport, target_comp)
                return df
    if excluded_hexes:
        before_n = int(df["hex_id"].nunique())
        df = df[~df["hex_id"].isin(excluded_hexes)].copy()
        after_n = int(df["hex_id"].nunique())
        if before_n != after_n:
            log.info("[%s] excluded %d obs hexes inside airport+ring "
                     "(%d → %d unique hexes in obs)",
                     airport, before_n - after_n, before_n, after_n)
    return df
