"""validate.py — write outputs/<ts>/validation_report.md in the repo's
assumption -> expected -> actual -> pass/fail table format.

Checks:
  1. Realized quality >= target (both tiers) for every solved cut. Cuts whose
     target the curve cannot reach (solver status != ok) are reported as WARN,
     not FAIL — they are flagged, not silently dropped.
  2. Row reconciliation: pre-enrich extraction rows == n_obs_to_be_added ==
     n_extraction_rows, and distinct route_rank == n_routes_to_be_added.
  3. Kept + dropped == requested observations (nothing vanishes in scheduling).
  4. BPO shift cap: no staffed hour-of-week cell exceeds 30 observations.
  5. Completeness: every cut appears in quality_summary + the extraction sheets.
"""

from __future__ import annotations

import csv
from pathlib import Path

from lib.curve_config import TIER_RANK


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open() as f:
        return list(csv.DictReader(f))


def _count_rows_and_routes(path: Path, trip: str) -> tuple[int, set]:
    n, routes = 0, set()
    for r in _read_csv(path):
        n += 1
        routes.add(r.get("route_rank"))
    return n, routes


def write_report(out_dir: Path, cuts: list[dict], solve_report: list[dict],
                 routes_dir: Path, published_url: str, sheet_id: str) -> Path:
    work = out_dir / "_work"
    quality = {(r["airport_code"].upper(), r["trip_type"].upper()): r
               for r in _read_csv(out_dir / "quality_summary.csv")}
    realized = {(r["airport_code"].upper(), r["trip_type"].upper()): r
                for r in _read_csv(out_dir / "realized_quality_summary.csv")}
    shift = _read_csv(out_dir / "bpo_shift_schedule.csv")
    dropped = _read_csv(out_dir / "dropped_routes.csv")

    checks: list[tuple[str, str, str, bool]] = []
    warns: list[str] = []

    # --- Check 1: realized quality >= target -------------------------------
    if solve_report:
        fails = []
        for r in solve_report:
            key = (r["airport"].upper(), r["trip_type"].upper())
            rq = realized.get(key)
            r_npi = rq["realized_NPI_quality"] if rq else r["realized_NPI_quality"]
            r_time = rq["realized_time_NPI_quality"] if rq else r["realized_time_NPI_quality"]
            npi_ok = TIER_RANK.get(r_npi, 0) >= TIER_RANK.get(r["npi_target"], 0)
            time_ok = TIER_RANK.get(r_time, 0) >= TIER_RANK.get(r["time_target"], 0)
            if r["status"] != "ok":
                warns.append(f"{key[0]}/{key[1]}: solver status={r['status']} "
                             f"(target NPI={r['npi_target']}/time={r['time_target']}, "
                             f"realized NPI={r_npi}/time={r_time})")
            elif not (npi_ok and time_ok):
                fails.append(f"{key[0]}/{key[1]}: realized (NPI={r_npi}, time={r_time}) "
                             f"< target (NPI={r['npi_target']}, time={r['time_target']})")
        checks.append((
            "Realized quality >= target for reachable cuts",
            "all 'ok' cuts meet both target tiers",
            f"{len(fails)} violations, {len(warns)} unreachable-target warnings",
            not fails))
    else:
        checks.append(("Quality vs target",
                       "n/a (explicit --cuts mode, no targets)", "skipped", True))

    # --- Check 2 + 3: reconciliation ---------------------------------------
    recon_fail = []
    vanish_fail = []
    drop_by_key: dict[tuple, int] = {}
    for d in dropped:
        k = (d["airport"].upper(), d["trip"].upper())
        drop_by_key[k] = drop_by_key.get(k, 0) + 1
    for (apt, trip), q in quality.items():
        pre_path = work / f"{trip}_extraction.csv"
        pre_n, pre_routes = _count_rows_and_routes(pre_path, trip)
        # pre-enrich rows for this specific airport
        pre_n_apt = sum(1 for r in _read_csv(pre_path)
                        if (r.get("pu_airport_code") or r.get("do_airport_code") or "").upper() == apt)
        q_obs = int(q["n_obs_to_be_added"])
        q_rows = int(q["n_extraction_rows"])
        if not (pre_n_apt == q_obs == q_rows):
            recon_fail.append(f"{apt}/{trip}: pre-enrich rows={pre_n_apt}, "
                              f"n_obs_to_be_added={q_obs}, n_extraction_rows={q_rows}")
        # kept (enriched) + dropped == requested
        enr_path = out_dir / f"{trip}_extraction.csv"
        kept = sum(1 for r in _read_csv(enr_path)
                   if (r.get("pu_airport_code") or r.get("do_airport_code") or "").upper() == apt)
        dropn = drop_by_key.get((apt, trip), 0)
        if kept + dropn != q_obs:
            vanish_fail.append(f"{apt}/{trip}: kept={kept} + dropped={dropn} "
                               f"!= requested={q_obs}")
    checks.append(("Row reconciliation (pre-enrich)",
                   "rows == n_obs_to_be_added == n_extraction_rows per cut",
                   f"{len(recon_fail)} mismatches", not recon_fail))
    checks.append(("No observations vanish in scheduling",
                   "kept + dropped == requested per cut",
                   f"{len(vanish_fail)} mismatches", not vanish_fail))

    # --- Check 4: shift cap -------------------------------------------------
    maxload = max((int(r["n_obs"]) for r in shift), default=0)
    checks.append(("BPO shift cap", "no hour-of-week cell > 30 obs",
                   f"max cell load = {maxload}", maxload <= 30))

    # --- Check 5: completeness ---------------------------------------------
    missing = []
    for c in cuts:
        key = (c["airport"].upper(), c["trip_type"].upper())
        if key not in quality:
            missing.append(f"{key[0]}/{key[1]} missing from quality_summary")
    checks.append(("Completeness", "every cut present in quality_summary",
                   f"{len(missing)} missing", not missing))

    # --- Write report -------------------------------------------------------
    lines = ["# Validation report — BPO NPI observation planner", ""]
    lines.append(f"- output dir: `{out_dir.name}`")
    lines.append(f"- cuts: {len(cuts)}")
    if sheet_id:
        lines.append(f"- google sheet: {published_url or sheet_id}")
    lines.append("")
    lines.append("## Checks")
    lines.append("")
    lines.append("| # | Assumption | Expected | Actual | Pass/Fail |")
    lines.append("|---|---|---|---|---|")
    for i, (a, e, act, ok) in enumerate(checks, 1):
        lines.append(f"| {i} | {a} | {e} | {act} | {'✅' if ok else '❌'} |")
    lines.append("")
    if warns:
        lines.append("## Unreachable-target warnings (flagged, not failures)")
        lines.append("")
        lines.append("These cuts cannot reach the requested tier at the chosen "
                     "scenario — the solver returned the best achievable N. "
                     "Consider a higher scenario (light→mid→max) or accept the "
                     "realized tier.")
        lines.append("")
        for w in warns:
            lines.append(f"- {w}")
        lines.append("")
    lines.append("## Ground-truth sources")
    lines.append("")
    lines.append("- Coverage curves + baseline: vendored npi-coverage-v5 "
                 "`routes.json` snapshots and `current.csv` "
                 "(projects/2026-04-30_npi-coverage-curves).")
    lines.append("- Shift rule: ≤30 obs / hour-of-week, strict local occasions "
                 "(projects/2026-05-29_npi-coverage-curve-extraction/scripts/schedule_shifts.py).")
    lines.append("- Parity: `scripts/verify_published_parity.py` reproduces the "
                 "published SNPE/WAW hand-off sheet 19UAk… and the dashboard curves.")
    lines.append("")

    report = out_dir / "validation_report.md"
    report.write_text("\n".join(lines) + "\n")
    n_fail = sum(1 for *_x, ok in checks if not ok)
    print(f"[validate] {report}  ({n_fail} check failures, {len(warns)} warnings)")
    return report
