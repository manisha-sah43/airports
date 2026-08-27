"""observations.py — load existing BPO-mapped observations, filter to the
canonical competitor, and (optionally) pull them fresh from the warehouse.

Two data paths:
  * pull_fresh(...)      run current_observations_emea_bpo_mapped.sql through
                         QueryRunner (lib/qr.py) over a lookback window, cache to
                         state/obs_<end>.csv, and return the DataFrame. This is
                         the default (Anton: "re-pull existing observations fresh").
  * load_csv(path)       read a frozen observations CSV (used for --obs-csv and
                         for the parity fixture).

filter_competitor(df, airport, map_path) keeps only the airport's canonical
competitor (CDG->Bolt, MAD->Cabify, ...). Airports mapped to 'Other' have no
comparable competitor and yield an empty frame (no baseline / no exclusion).

Grain of the returned frame (one row per):
  airport_code, trip_type, hex_id, time_bucket, dist_bucket, competitor, num_observations
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd

_OBS_COLS = ("airport_code", "trip_type", "hex_id", "time_bucket",
             "dist_bucket", "competitor", "num_observations")


def _coerce(df: pd.DataFrame) -> pd.DataFrame:
    for c in ("airport_code", "trip_type", "hex_id", "time_bucket",
              "dist_bucket", "competitor"):
        if c in df.columns:
            df[c] = df[c].astype(str)
    if "num_observations" in df.columns:
        df["num_observations"] = (pd.to_numeric(df["num_observations"],
                                                errors="coerce")
                                    .fillna(0).astype(int))
    return df


def load_csv(path: Path) -> pd.DataFrame:
    if not path.exists():
        raise SystemExit(f"[obs] observations CSV not found: {path}")
    return _coerce(pd.read_csv(path))


def filter_competitor(df: pd.DataFrame, airport: str,
                      competitor_map_path: Path) -> pd.DataFrame:
    """Keep only the airport's canonical competitor. Empty frame if 'Other'."""
    if df.empty or "competitor" not in df.columns:
        return df
    amap = json.loads(competitor_map_path.read_text()) if competitor_map_path.exists() else {}
    target = amap.get(airport.upper())
    if not target:
        # No mapping: keep all (may double-count) — surfaced by validation.
        return df
    if str(target).lower() == "other":
        return df.iloc[0:0]
    return df[df["competitor"] == target].copy()


def pull_fresh(sql_path: Path, start_date: str, end_date: str,
               hex_size: int, cache_path: Path) -> pd.DataFrame:
    """Run the vendored SQL through QueryRunner and cache the result CSV."""
    # lib/qr.py is stdlib-only and self-contained.
    from lib import qr

    sql = sql_path.read_text()
    sql = "\n".join(ln for ln in sql.splitlines()
                    if not ln.strip().lower().startswith("set session"))
    for k, v in (("{{start_date}}", start_date),
                 ("{{end_date}}", end_date),
                 ("{{hex_size}}", str(hex_size))):
        sql = sql.replace(k, v)

    status, rows, cols, uuid = qr.run_query(sql, "current_observations")
    if status != "completed_success":
        raise SystemExit(f"[obs] fresh pull failed: status={status} uuid={uuid}")
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    qr.write_csv(str(cache_path), cols, rows)
    print(f"[obs] fresh pull: {len(rows)} rows -> {cache_path}  (uuid={uuid})",
          file=sys.stderr)
    return _coerce(pd.read_csv(cache_path))
