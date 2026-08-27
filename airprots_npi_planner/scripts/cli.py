"""cli.py — orchestrate: targets -> solve -> obs -> extraction rows -> shifts ->
enrich -> realized quality -> publish -> validate.

Run via ../run.py (which bootstraps the .venv). All paths resolve relative to the
asset root, so it works from any cwd.
"""

from __future__ import annotations

import argparse
import csv
import json
import shutil
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

ASSET_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ASSET_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))          # so `import lib.x`, `import observations` work

import pandas as pd  # noqa: E402

import observations  # noqa: E402
import build_extraction_list as bel  # noqa: E402
import schedule_shifts  # noqa: E402
import enrich_extraction  # noqa: E402
import geocode_addresses  # noqa: E402
import recompute_realized_quality as rrq  # noqa: E402
from solve_targets import solve_targets  # noqa: E402
import validate as validate_mod  # noqa: E402


# ------------------------------------------------------------------- config ---

def _load_config(path: Path) -> dict:
    cfg = json.loads(path.read_text()) if path.exists() else {}
    cfg.setdefault("lookback_weeks", 4)
    cfg.setdefault("hex_size", 7)
    cfg.setdefault("min_obs", 1)
    cfg.setdefault("gsheet_id", "")
    cfg.setdefault("routes_dir", "config/routes_2026-05-12")
    cfg.setdefault("demand_lut_dir", "config/demand_lut_2026-05-12")
    cfg.setdefault("geocode", True)
    # Route-generation (Step 0). Default OFF: consume the vendored snapshot.
    cfg.setdefault("regenerate_routes", False)
    cfg.setdefault("regenerate_scope", "targets")  # "targets" | "all"
    # Route exclusion source. Default OFF = bug-compatible (reproduce the shipped
    # snapshot: routes use the 15-entry route-gen coords, LUTs the complete ones).
    # ON = apply the complete airport-terminal exclusion to routes too (the
    # corrected refresh; changes ~178 airports' curves).
    cfg.setdefault("regenerate_full_exclusion", False)
    cfg.setdefault("demand_csv", "config/demand_2026-05-12/emea_demand_full.csv")
    cfg.setdefault("demand_obs_csv",
                   "config/demand_2026-05-12/emea_observations_bpo_mapped.csv")
    cfg.setdefault("demand_snapshot_date", "2026-05-12")
    return cfg


def _last_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def _window(cfg: dict, end_override: str | None) -> tuple[str, str]:
    """Return (start_date incl, end_date excl) for the fresh obs pull."""
    end = (datetime.strptime(end_override, "%Y-%m-%d").date()
           if end_override else _last_monday(date.today()))
    start = end - timedelta(weeks=int(cfg["lookback_weeks"]))
    return start.isoformat(), end.isoformat()


# -------------------------------------------------------------------- inputs ---

def _read_targets(path: Path) -> list[dict]:
    req = ("airport_code", "trip_type", "npi_quality", "time_npi_quality")
    rows = []
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        norm = {(c.lower().strip() if c else c): c for c in (reader.fieldnames or [])}
        # tolerate 'trip_duration' (Anton's sheet header) as an alias for trip_type
        if "trip_type" not in norm and "trip_duration" in norm:
            norm["trip_type"] = norm["trip_duration"]
        for col in req:
            if col not in norm:
                raise SystemExit(f"targets CSV missing column: {col!r}")
        has_scen = "scenario" in norm  # optional override
        for raw in reader:
            r = {c: (raw[norm[c]] or "").strip() for c in req}
            if not r["airport_code"]:
                continue
            rows.append({
                "airport": r["airport_code"].upper(),
                "trip_type": r["trip_type"].upper(),
                "npi_quality": r["npi_quality"],
                "time_npi_quality": r["time_npi_quality"],
                "scenario": (raw[norm["scenario"]] or "").strip().lower() if has_scen else "",
            })
    if not rows:
        raise SystemExit("no target rows in " + str(path))
    return rows


def _read_cuts(path: Path) -> list[dict]:
    req = ("airport", "trip_type", "scenario", "n_routes")
    rows = []
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        norm = {(c.lower().strip() if c else c): c for c in (reader.fieldnames or [])}
        for col in req:
            if col not in norm:
                raise SystemExit(f"cuts CSV missing column: {col!r}")
        for raw in reader:
            r = {c: (raw[norm[c]] or "").strip() for c in req}
            if not r["airport"]:
                continue
            rows.append({"airport": r["airport"].upper(),
                         "trip_type": r["trip_type"].upper(),
                         "scenario": r["scenario"].lower(),
                         "n_routes": int(r["n_routes"])})
    if not rows:
        raise SystemExit("no cut rows in " + str(path))
    return rows


def _write_dicts(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("")
        return
    cols = list(rows[0].keys())
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


# --------------------------------------------------------------------- main ---

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="BPO NPI observation planner")
    ap.add_argument("--targets", type=Path,
                    default=ASSET_ROOT / "inputs" / "targets.csv",
                    help="quality targets: airport_code,trip_type,scenario,npi_quality,time_npi_quality")
    ap.add_argument("--cuts", type=Path, default=None,
                    help="skip the solver: airport,trip_type,scenario,n_routes")
    ap.add_argument("--config", type=Path,
                    default=ASSET_ROOT / "inputs" / "config.json")
    ap.add_argument("--obs-csv", type=Path, default=None,
                    help="use a frozen observations CSV instead of a fresh pull")
    ap.add_argument("--end-date", default=None,
                    help="obs window end (excl) YYYY-MM-DD; default last Monday")
    ap.add_argument("--gsheet-id", default=None, help="override config gsheet_id")
    ap.add_argument("--no-publish", action="store_true")
    ap.add_argument("--no-geocode", action="store_true",
                    help="skip network geocoding (use cache only)")
    ap.add_argument("--regenerate-routes", dest="regenerate_routes",
                    action="store_true", default=None,
                    help="Step 0: rebuild routes/LUTs from the vendored demand "
                         "CSV before solving (overrides config.regenerate_routes)")
    ap.add_argument("--no-regenerate", dest="regenerate_routes",
                    action="store_false",
                    help="force consuming the vendored snapshot (overrides config)")
    ap.add_argument("--out-dir", type=Path, default=None)
    args = ap.parse_args(argv)

    cfg = _load_config(args.config)
    routes_dir = ASSET_ROOT / cfg["routes_dir"]
    lut_dir = ASSET_ROOT / cfg["demand_lut_dir"]
    coords_path = ASSET_ROOT / "config" / "airport_coords.json"
    route_coords_path = ASSET_ROOT / "config" / "airport_coords_routegen.json"
    city_id_path = ASSET_ROOT / "config" / "airport_city_id.json"
    competitor_path = ASSET_ROOT / "config" / "airport_competitor_mapping.json"
    current_csv = ASSET_ROOT / "config" / "dashboard_baseline" / "current.csv"
    geocode_cache = ASSET_ROOT / "config" / "geocode_cache.json"
    airport_cache = ASSET_ROOT / "config" / "airport_geocode_cache.json"
    sql_path = SCRIPTS / "queries" / "current_observations_emea_bpo_mapped.sql"

    ts = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    out_dir = args.out_dir or (ASSET_ROOT / "outputs" / ts)
    work_dir = out_dir / "_work"
    out_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    coords_map = json.loads(coords_path.read_text())
    city_id_raw = json.loads(city_id_path.read_text())
    city_id_map = {k.upper(): int(v["city_id"] if isinstance(v, dict) else v)
                   for k, v in city_id_raw.items()}

    # 0) (Optional) Regenerate the coverage curves from the vendored demand CSV.
    #    Default OFF — the pipeline consumes the vendored snapshot. When enabled
    #    (config.regenerate_routes or --regenerate-routes), rebuild routes.json +
    #    demand LUTs (+ a snapshot-local current.csv) into a dated snapshot dir
    #    and point the rest of the run at them. See scripts/routegen/.
    regen = (args.regenerate_routes if args.regenerate_routes is not None
             else bool(cfg.get("regenerate_routes")))
    if regen:
        import routegen.build_routes as build_routes
        gen_date = cfg.get("demand_snapshot_date") or date.today().isoformat()
        routes_dir = ASSET_ROOT / f"config/routes_{gen_date}"
        lut_dir = ASSET_ROOT / f"config/demand_lut_{gen_date}"
        demand_csv = ASSET_ROOT / cfg["demand_csv"]
        demand_obs = ASSET_ROOT / cfg["demand_obs_csv"]
        scope = cfg.get("regenerate_scope", "targets")
        if scope == "all":
            gen_airports = None
        elif args.cuts:
            gen_airports = sorted({r["airport"] for r in _read_cuts(args.cuts)})
        else:
            gen_airports = sorted({r["airport"] for r in _read_targets(args.targets)})
        current_csv = routes_dir / "current.csv"   # snapshot-local baseline
        run_dt = (datetime.strptime(gen_date, "%Y-%m-%d").date()
                  if cfg.get("demand_snapshot_date") else None)
        full_excl = bool(cfg.get("regenerate_full_exclusion"))
        print(f"[cli] Step 0: regenerating routes "
              f"({'ALL airports' if gen_airports is None else ', '.join(gen_airports)}) "
              f"from {demand_csv.name} -> {routes_dir.name} "
              f"[exclusion={'full/corrected' if full_excl else 'bug-compatible'}]")
        build_routes.build(
            demand_csv=demand_csv, obs_csv=demand_obs, coords_path=coords_path,
            route_coords_path=route_coords_path, full_exclusion=full_excl,
            competitor_path=competitor_path, out_routes_dir=routes_dir,
            out_lut_dir=lut_dir, current_csv_path=current_csv,
            airports=gen_airports, hex_size=int(cfg["hex_size"]), run_dt=run_dt)
        print(f"[cli] Step 0 done -> routes={routes_dir} lut={lut_dir} "
              f"current={current_csv}")

    # 1) Resolve cuts -----------------------------------------------------------
    solve_report: list[dict] = []
    if args.cuts:
        cuts = _read_cuts(args.cuts)
        print(f"[cli] using {len(cuts)} explicit cuts from {args.cuts}")
    else:
        targets = _read_targets(args.targets)
        solve_report = solve_targets(routes_dir, targets)
        cuts = [{"airport": r["airport"], "trip_type": r["trip_type"],
                 "scenario": r["scenario"], "n_routes": r["n_routes"]}
                for r in solve_report]
        _write_dicts(out_dir / "resolved_cuts.csv", solve_report)
        n_flag = sum(1 for r in solve_report if r["status"] != "ok")
        print(f"[cli] solved {len(cuts)} cuts from targets "
              f"({n_flag} with unreachable-target flags) -> resolved_cuts.csv")

    # 2) Observations -----------------------------------------------------------
    if args.obs_csv:
        obs_df = observations.load_csv(args.obs_csv)
        print(f"[cli] loaded {len(obs_df)} obs rows from {args.obs_csv}")
    else:
        start, end = _window(cfg, args.end_date)
        cache = ASSET_ROOT / "state" / f"obs_{end}.csv"
        if cache.exists():
            obs_df = observations.load_csv(cache)
            print(f"[cli] reusing cached obs {cache} ({len(obs_df)} rows)")
        else:
            print(f"[cli] pulling fresh observations {start}..{end} (excl)")
            obs_df = observations.pull_fresh(sql_path, start, end,
                                             int(cfg["hex_size"]), cache)

    # 3) Extraction rows + planned quality -------------------------------------
    stats = bel.build(cuts, routes_dir, obs_df, coords_map, city_id_map,
                      current_csv, int(cfg["min_obs"]), competitor_path, work_dir)
    print(f"[cli] built extraction rows: PU={stats['pu']} DO={stats['do']} "
          f"cuts={stats['cuts']}")

    # 4) Shift distribution -----------------------------------------------------
    schedule_shifts.main(["--baseline-dir", str(work_dir),
                          "--airport-coords", str(coords_path),
                          "--out-dir", str(out_dir)])

    # 5) Geocode city-side addresses (cache-first) ------------------------------
    if cfg.get("geocode", True) and not args.no_geocode:
        try:
            geocode_addresses.main(["--baseline-dir", str(work_dir),
                                    "--cache", str(geocode_cache)])
        except Exception as e:  # noqa: BLE001 — network best-effort
            print(f"[cli] geocode step skipped ({type(e).__name__}: {e})",
                  file=sys.stderr)

    # 6) Enrich (addresses + UTC shift columns) ---------------------------------
    enrich_extraction.main(["--baseline-dir", str(work_dir),
                            "--cache", str(geocode_cache),
                            "--airport-cache", str(airport_cache),
                            "--airport-coords", str(coords_path),
                            "--assignment", str(out_dir / "row_assignment.json"),
                            "--out-dir", str(out_dir)])

    # 7) Realized quality after trim -------------------------------------------
    rrq.recompute(cuts, routes_dir, lut_dir,
                  out_dir / "dropped_routes.csv",
                  out_dir / "realized_quality_summary.csv")

    # 8) Planned quality_summary alongside (transparency) ----------------------
    shutil.copy(work_dir / "quality_summary.csv", out_dir / "quality_summary.csv")

    # 9) Publish ----------------------------------------------------------------
    sheet_id = args.gsheet_id or cfg.get("gsheet_id") or ""
    published_url = ""
    if not args.no_publish:
        if not sheet_id:
            print("[cli] no gsheet_id configured; skipping publish "
                  "(set inputs/config.json gsheet_id or pass --gsheet-id)",
                  file=sys.stderr)
        else:
            import publish_sheet
            published_url = publish_sheet.publish(out_dir, sheet_id)

    # 10) Validate --------------------------------------------------------------
    validate_mod.write_report(out_dir, cuts, solve_report, routes_dir,
                              published_url, sheet_id)

    print(f"\n[cli] DONE -> {out_dir}")
    if published_url:
        print(f"[cli] Sheet: {published_url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
