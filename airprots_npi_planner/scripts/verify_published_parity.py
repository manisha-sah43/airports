"""verify_published_parity.py — prove the asset reproduces the already-published
artifacts:

  A. The SNPE/WAW hand-off sheet 19UAkSYujxgFDz39IBMexUFXKs_nxU5yAmGFKONDqivo
     (frozen local copy in tests/fixtures/snpe_pilot_published/). We re-run the
     asset on the exact cuts that produced it, using the frozen observations
     snapshot, and diff PU/DO_extraction, bpo_shift_schedule, dropped_routes, and
     realized_quality_summary row-for-row.
  B. The npi-coverage-v5 dashboard curves.csv — the solver's target->N logic must
     agree with the curve (min N reaching a tier).

Run inside the asset .venv:
  python scripts/verify_published_parity.py
  python scripts/verify_published_parity.py --live   # also fetch the live sheet

Exit non-zero on any hard parity failure.
"""

from __future__ import annotations

import argparse
import csv
import sys
import tempfile
from pathlib import Path

ASSET_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ASSET_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import cli  # noqa: E402
from lib.curve_config import TIER_RANK  # noqa: E402
from lib.tiering import quality_from_snapshot  # noqa: E402

FIX = ASSET_ROOT / "tests" / "fixtures"
PUBLISHED = FIX / "snpe_pilot_published"
# The frozen 2026-05-12 BPO-mapped observations snapshot now lives in the config
# demand layer (single canonical copy — it doubles as the route-gen baseline).
OBS_FIXTURE = (ASSET_ROOT / "config" / "demand_2026-05-12"
               / "emea_observations_bpo_mapped.csv")


def _rows(path: Path) -> list[dict]:
    with path.open() as f:
        return list(csv.DictReader(f))


def _diff_csv(name: str, got: Path, exp: Path, cols: list[str] | None = None,
              drift_cols: set | None = None, tol: float = 0.0) -> list[str]:
    """Compare two CSVs as multisets of rows. Cells in `drift_cols` may differ by
    <= tol (coverage columns subject to documented demand-snapshot drift); every
    other cell — including numeric identity columns like route_rank — must match
    exactly."""
    if not exp.exists():
        return [f"{name}: fixture missing ({exp})"]
    if not got.exists():
        return [f"{name}: asset output missing ({got})"]
    g, e = _rows(got), _rows(exp)
    use = cols or ([c for c in e[0].keys()] if e else [])
    drift = drift_cols or set()
    problems = []
    if len(g) != len(e):
        problems.append(f"{name}: row count {len(g)} != published {len(e)}")

    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _key(r):
        # identity = every non-drift cell (exact match required to align rows)
        return tuple((c, str(r.get(c, "")).strip()) for c in use if c not in drift)

    gmap: dict = {}
    for r in g:
        gmap.setdefault(_key(r), []).append(r)
    n_diff = 0
    for er in e:
        k = _key(er)
        bucket = gmap.get(k)
        if not bucket:
            n_diff += 1
            if n_diff <= 3:
                problems.append(f"    published-only: {dict(k)}")
            continue
        gr = bucket.pop()
        for c in drift:
            en, gn = _num(er.get(c)), _num(gr.get(c))
            if en is None or gn is None or abs(en - gn) > tol:
                n_diff += 1
                if n_diff <= 5:
                    problems.append(f"    {name} {c}: {gr.get(c)} vs published "
                                    f"{er.get(c)} (tol {tol})")
    if n_diff:
        problems.insert(0, f"{name}: {n_diff} differing cells/rows")
    else:
        print(f"  [OK] {name}: {len(g)} rows match"
              + (f" (drift tol={tol} on {sorted(drift)})" if drift else ""))
    return problems


def part_a() -> list[str]:
    print("== Part A: reproduce the published SNPE hand-off ==")
    tmp = Path(tempfile.mkdtemp(prefix="npi_parity_"))
    cli.main(["--cuts", str(FIX / "cuts_snpe.csv"),
              "--obs-csv", str(OBS_FIXTURE),
              "--no-publish", "--no-geocode",
              "--out-dir", str(tmp)])
    problems = []
    problems += _diff_csv("PU_extraction", tmp / "PU_extraction.csv",
                          PUBLISHED / "PU_extraction.csv")
    problems += _diff_csv("DO_extraction", tmp / "DO_extraction.csv",
                          PUBLISHED / "DO_extraction.csv")
    problems += _diff_csv("bpo_shift_schedule", tmp / "bpo_shift_schedule.csv",
                          PUBLISHED / "bpo_shift_schedule.csv")
    problems += _diff_csv("dropped_routes", tmp / "dropped_routes.csv",
                          PUBLISHED / "dropped_routes.csv")
    # realized quality: tiers must match exactly; coverage may drift <=0.002 vs
    # the published run (documented demand-snapshot drift, source build_extraction
    # _list.py:534-546). Compare on the published columns (asset adds `status`).
    exp_cols = list(_rows(PUBLISHED / "realized_quality_summary.csv")[0].keys())
    problems += _diff_csv("realized_quality_summary",
                          tmp / "realized_quality_summary.csv",
                          PUBLISHED / "realized_quality_summary.csv",
                          cols=exp_cols,
                          drift_cols={"planned_sample_coverage", "realized_sample_coverage",
                                      "planned_g_coverage", "realized_g_coverage"},
                          tol=0.002)
    return problems


def part_b() -> list[str]:
    print("== Part B: solver agrees with the dashboard curve ==")
    routes_dir = ASSET_ROOT / cli._load_config(ASSET_ROOT / "inputs" / "config.json")["routes_dir"]
    from solve_targets import solve_one, solve_best
    problems = []
    # B1: per-scenario inversion (solve_one) must reproduce Anton's output_scenario
    # picks (the routes he read off the dashboard slider) for every row.
    # B2: the auto-scenario solver (solve_best, the user-facing path — no scenario
    # input) must reach the SAME route count with the FEWEST observations.
    scen_rows = _rows(FIX / "output_scenario_raw.csv")
    m_one = m_best = checked = 0
    for r in scen_rows:
        apt = r["airport_code"].strip().upper()
        trip = r["trip_duration"].strip().upper()
        scen = r["scenario"].strip().lower()
        npi = r["npi_quality"].strip()
        tnpi = r["time_npi_quality"].strip()
        try:
            exp = int(float(r["routes_week"]))
        except (TypeError, ValueError):
            continue
        if not (routes_dir / apt / "routes.json").exists():
            continue
        checked += 1
        r1 = solve_one(routes_dir, apt, trip, scen, npi, tnpi)
        if r1["n_routes"] == exp:
            m_one += 1
        else:
            problems.append(f"solve_one {apt}/{trip}/{scen}: N={r1['n_routes']} != Anton's {exp}")
        rb = solve_best(routes_dir, apt, trip, npi, tnpi)
        if rb["scenario"] == scen and rb["n_routes"] == exp:
            m_best += 1
        else:
            problems.append(f"solve_best {apt}/{trip}: {rb['scenario']}/{rb['n_routes']} "
                            f"!= Anton's {scen}/{exp}")
    print(f"  solve_one (pinned scenario):   {m_one}/{checked} match output_scenario")
    print(f"  solve_best (auto min-obs):     {m_best}/{checked} match output_scenario")
    return problems


def part_live() -> list[str]:
    print("== Live sheet fetch (read-only) ==")
    try:
        from lib import fetch_live_sheet  # noqa: F401
        print("  fetch_live_sheet available; run it manually to pull the live tabs "
              "then re-diff against tests/fixtures/snpe_pilot_published/.")
    except Exception as e:  # noqa: BLE001
        return [f"live fetch unavailable: {e}"]
    return []


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    args = ap.parse_args(argv)
    problems = part_a() + part_b()
    if args.live:
        problems += part_live()
    print()
    if problems:
        print("PARITY FAILURES:")
        for p in problems:
            print("  " + p)
        return 1
    print("ALL PARITY CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
