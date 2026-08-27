"""solve_targets.py — invert the coverage curve: required quality -> min observations.

This is the automation of what a human does on the npi-coverage-v5 dashboard:
drag the route slider until the quality chip reaches the target tier, then read
off the routes/observations. Here we walk the pre-computed coverage snapshots in
routes.json and pick the plan that meets both the NPI target and the time-NPI
target with the FEWEST observations.

The end user supplies only the airport + the two quality tiers. `scenario`
(θ = how many occasions each route covers: light/mid/max) is an internal knob:
`solve_best` searches all three and returns the cheapest feasible one, so the
user never has to reason about it. (A scenario may still be pinned per row as an
optional override — e.g. to reproduce a historical cut.)

Per-scenario selection rule (`solve_one`, matches the dashboard slider):
  Walk snapshots by ascending n_routes; take the first N where NPI_quality >=
  npi_target AND time_NPI_quality >= time_target.
  - both reachable            -> that N, status "ok"
  - only NPI reachable        -> min N meeting NPI, status "time_target_unreachable"
  - neither reachable at maxN  -> max N, status "npi_target_unreachable"

`solve_best` then picks, across scenarios: the cheapest (fewest observations)
"ok" plan; else the cheapest plan that at least meets the NPI target; else the
best-quality plan. Everything is flagged, never silent.
"""

from __future__ import annotations

import json
from pathlib import Path

from lib.curve_config import TIER_RANK, VALID_SCENARIOS, SCENARIOS
from lib.tiering import snapshot_at, quality_from_snapshot

# scenarios cheapest-θ first (light covers fewest occasions/route)
_SCENARIO_ORDER = ("light", "mid", "max")


def _load_snapshots(routes_dir: Path, airport: str, trip: str,
                    scenario: str) -> list[dict]:
    rj = routes_dir / airport / "routes.json"
    if not rj.exists():
        raise SystemExit(f"[solve] no routes.json for {airport}: {rj}")
    d = json.loads(rj.read_text())
    block = (d.get("trip_types", {}) or {}).get(trip, {}) or {}
    return (block.get("snapshots", {}) or {}).get(scenario, []) or []


def solve_one(routes_dir: Path, airport: str, trip: str, scenario: str,
              npi_target: str, time_target: str) -> dict:
    """Resolve one target row to a cut. Returns a dict with n_routes + status."""
    if scenario not in VALID_SCENARIOS:
        raise SystemExit(
            f"[solve] {airport}/{trip}: scenario {scenario!r} not in {sorted(VALID_SCENARIOS)}")
    for tier in (npi_target, time_target):
        if tier not in TIER_RANK:
            raise SystemExit(
                f"[solve] {airport}/{trip}: quality {tier!r} not in {list(TIER_RANK)}")

    snaps = sorted(_load_snapshots(routes_dir, airport, trip, scenario),
                   key=lambda s: int(s["n_routes"]))
    if not snaps:
        raise SystemExit(f"[solve] no snapshots for {airport}/{trip}/{scenario}")

    want_npi = TIER_RANK[npi_target]
    want_time = TIER_RANK[time_target]

    first_both = None      # min N meeting BOTH targets
    first_npi = None       # min N meeting the NPI target
    for s in snaps:
        _, _, q_npi, q_time = quality_from_snapshot(s)
        ok_npi = TIER_RANK[q_npi] >= want_npi
        ok_time = TIER_RANK[q_time] >= want_time
        if ok_npi and first_npi is None:
            first_npi = s
        if ok_npi and ok_time and first_both is None:
            first_both = s
            break

    if first_both is not None:
        chosen, status = first_both, "ok"
    elif first_npi is not None:
        chosen, status = first_npi, "time_target_unreachable"
    else:
        chosen, status = snaps[-1], "npi_target_unreachable"

    n_routes = int(chosen["n_routes"])
    r_s, r_g, r_npi, r_time = quality_from_snapshot(chosen)
    return {
        "airport": airport,
        "trip_type": trip,
        "scenario": scenario,
        "n_routes": n_routes,
        "n_observations_planned": int(chosen.get("n_observations", 0)),
        "npi_target": npi_target,
        "time_target": time_target,
        "realized_NPI_quality": r_npi,
        "realized_time_NPI_quality": r_time,
        "sample_coverage": round(r_s, 4),
        "geo_coverage": round(r_g, 4),
        "n_routes_available": int(snaps[-1]["n_routes"]),
        "status": status,
    }


def solve_best(routes_dir: Path, airport: str, trip: str,
               npi_target: str, time_target: str,
               scenarios: tuple[str, ...] = _SCENARIO_ORDER) -> dict:
    """Pick the plan that meets both targets with the FEWEST observations,
    searching across scenarios so the user never specifies θ.

    Preference order:
      1. cheapest (min observations, then min routes) plan with status "ok";
      2. else cheapest plan that at least meets the NPI target
         (status "time_target_unreachable");
      3. else the plan with the best achievable tiers (status "npi_target_unreachable").
    """
    per = [solve_one(routes_dir, airport, trip, scen, npi_target, time_target)
           for scen in scenarios]
    cost = lambda r: (r["n_observations_planned"], r["n_routes"])

    both = [r for r in per if r["status"] == "ok"]
    if both:
        return min(both, key=cost)
    npi_ok = [r for r in per if r["status"] in ("ok", "time_target_unreachable")]
    if npi_ok:
        return min(npi_ok, key=cost)
    # nothing meets the NPI target — return the best achievable quality.
    return max(per, key=lambda r: (TIER_RANK[r["realized_NPI_quality"]],
                                   TIER_RANK[r["realized_time_NPI_quality"]],
                                   -r["n_observations_planned"]))


def solve_targets(routes_dir: Path, targets: list[dict]) -> list[dict]:
    """targets = [{airport, trip_type, npi_quality, time_npi_quality,
                   scenario?(optional override)}, ...].

    When `scenario` is blank/absent the cheapest feasible scenario is chosen
    automatically (solve_best); when pinned, only that scenario is used (solve_one).
    """
    out = []
    for t in targets:
        scen = (t.get("scenario") or "").strip().lower()
        if scen:
            out.append(solve_one(
                routes_dir, t["airport"].upper(), t["trip_type"].upper(), scen,
                t["npi_quality"].strip(), t["time_npi_quality"].strip()))
        else:
            out.append(solve_best(
                routes_dir, t["airport"].upper(), t["trip_type"].upper(),
                t["npi_quality"].strip(), t["time_npi_quality"].strip()))
    return out
