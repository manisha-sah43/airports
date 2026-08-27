"""Compute current sample/geo coverage from a (demand, observations) pair.

Used by `refresh_baseline.py`: given the EMEA demand cells and the EMEA
current observations, compute, per (airport, trip_type), where each airport
sits today on its own greedy curve.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


@dataclass(frozen=True)
class TodayCoverage:
    n_observations_4w: int
    n_unique_routes_today: int
    sample_coverage_today: float
    geo_coverage_today: float


def compute_today_coverage(demand_cells: pd.DataFrame,
                           observations: pd.DataFrame) -> TodayCoverage:
    """For one (airport, trip_type): aggregate counts and project obs onto the
    same denominators the greedy uses (so the dot lies on the same axes as
    the curve).

    demand_cells columns: hex_id, time_bucket, dist_bucket, sessions
    observations columns: hex_id, time_bucket, dist_bucket, num_observations
    """
    if demand_cells.empty:
        return TodayCoverage(
            n_observations_4w=int(observations["num_observations"].sum()) if not observations.empty else 0,
            n_unique_routes_today=int(observations["hex_id"].nunique()) if not observations.empty else 0,
            sample_coverage_today=0.0,
            geo_coverage_today=0.0,
        )

    total = float(demand_cells["sessions"].sum())
    if total <= 0:
        return TodayCoverage(0, 0, 0.0, 0.0)

    # Sample weight per (time_bucket, dist_bucket).
    sample_w = (demand_cells.groupby(["time_bucket", "dist_bucket"], as_index=False)["sessions"]
                .sum())
    sample_w["weight"] = sample_w["sessions"] / total
    # Geo weight per hex.
    geo_w = (demand_cells.groupby("hex_id", as_index=False)["sessions"]
             .sum())
    geo_w["weight"] = geo_w["sessions"] / total

    n_obs = int(observations["num_observations"].sum()) if not observations.empty else 0
    n_unique_routes = int(observations["hex_id"].nunique()) if not observations.empty else 0

    if observations.empty:
        return TodayCoverage(n_obs, n_unique_routes, 0.0, 0.0)

    # Sample covered: any (t, d) cell that has >= 1 observation.
    covered_td = set(map(tuple, observations[["time_bucket", "dist_bucket"]]
                         .drop_duplicates().values.tolist()))
    sample_cov = float(sample_w[
        sample_w.set_index(["time_bucket", "dist_bucket"]).index.isin(covered_td)
    ]["weight"].sum())

    # Geo covered: any hex with >= 1 observation.
    covered_hex = set(observations["hex_id"].astype(str).unique())
    geo_cov = float(geo_w[geo_w["hex_id"].astype(str).isin(covered_hex)]["weight"].sum())

    return TodayCoverage(
        n_observations_4w=n_obs,
        n_unique_routes_today=n_unique_routes,
        sample_coverage_today=min(sample_cov, 1.0),
        geo_coverage_today=min(geo_cov, 1.0),
    )
