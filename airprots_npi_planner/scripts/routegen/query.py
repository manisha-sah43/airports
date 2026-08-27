"""Presto execution wrappers for the coverage demand SQL.

Refresh-only: the default pipeline reads the vendored demand snapshot
(`config/demand_<date>/emea_demand_full.csv`); this module is used only when a
maintainer wants to re-pull demand from the warehouse to refresh that snapshot.

Vendored from shared/scripts/coverage_curve/query.py — `.config` import replaced
by local constants (DEFAULT_HEX_SIZE) and the asset's own `scripts/queries/` dir.
"""

from __future__ import annotations

import logging
import time
from datetime import date
from pathlib import Path

import pandas as pd

DEFAULT_HEX_SIZE = 7
# The asset vendors its SQL under scripts/queries/ (this file lives in
# scripts/routegen/, so the sibling `queries` dir is one level up).
QUERIES_DIR = Path(__file__).resolve().parent.parent / "queries"

log = logging.getLogger(__name__)

MAX_RETRIES = 1
RETRY_BACKOFF_SECONDS = 30


def _execute_with_retry(user_email: str, sql: str, label: str) -> pd.DataFrame:
    from queryrunner_client import Client                                     # lazy

    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            log.info("[%s] attempt %d submitting …", label, attempt)
            client = Client(user_email=user_email)
            cursor = client.execute("presto", sql)
            df = cursor.to_pandas()
            log.info("[%s] returned %d rows", label, len(df))
            return df
        except Exception as exc:
            last_exc = exc
            log.warning("[%s] attempt %d failed: %s", label, attempt, exc)
            if attempt <= MAX_RETRIES:
                time.sleep(RETRY_BACKOFF_SECONDS)
    raise RuntimeError(f"Query failed for {label} after {MAX_RETRIES + 1} attempts: {last_exc}") from last_exc


def _render(template: str, **params: object) -> str:
    out = template
    for k, v in params.items():
        out = out.replace("{{" + k + "}}", str(v))
    return out


def _load(name: str) -> str:
    return (QUERIES_DIR / name).read_text()


def fetch_session_demand_emea(start_date: date, end_date: date,
                              user_email: str,
                              hex_size: int = DEFAULT_HEX_SIZE,
                              include_non_operational: bool = False) -> pd.DataFrame:
    sql = _render(
        _load("airport_session_demand_emea.sql"),
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        hex_size=hex_size,
        child_hex_size=hex_size + 1,
        operational_clause=("" if include_non_operational
                            else "and a.is_operational and not a.is_deleted"),
    )
    label = "demand[EMEA+nonop]" if include_non_operational else "demand[EMEA]"
    df = _execute_with_retry(user_email, sql, label)
    return _coerce_demand(df)


def _coerce_observations(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    if "num_observations" in df.columns:
        df["num_observations"] = pd.to_numeric(df["num_observations"], errors="coerce").fillna(0).astype(int)
    for col in ("airport_code", "trip_type", "hex_id", "time_bucket", "dist_bucket"):
        if col in df.columns:
            df[col] = df[col].astype(str)
    return df
