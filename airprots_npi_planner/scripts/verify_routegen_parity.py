"""verify_routegen_parity.py — prove the in-asset route generator reproduces the
shipped snapshot.

Regenerates routes.json + demand LUTs from the vendored demand CSV
(config/demand_2026-05-12/) and asserts field-for-field equality against the
vendored snapshot (config/routes_2026-05-12/ + config/demand_lut_2026-05-12/).

This is the ground-truth check for the route-generation stage: if it passes, the
vendored generator faithfully rebuilds the artifact the asset ships, so a team
that regenerates (or refreshes) the curves gets the same pipeline behaviour.

Usage:
  python scripts/verify_routegen_parity.py                 # all vendored airports
  python scripts/verify_routegen_parity.py --airports CDG,LHR,BCN
  python scripts/verify_routegen_parity.py --sample 20     # first 20 (sorted)

Exit code 0 = all parity checks pass; 1 = any mismatch.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import date
from pathlib import Path

ASSET_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ASSET_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import routegen.build_routes as build_routes  # noqa: E402

VENDORED_ROUTES = ASSET_ROOT / "config" / "routes_2026-05-12"
VENDORED_LUT = ASSET_ROOT / "config" / "demand_lut_2026-05-12"
DEMAND_CSV = ASSET_ROOT / "config" / "demand_2026-05-12" / "emea_demand_full.csv"
OBS_CSV = ASSET_ROOT / "config" / "demand_2026-05-12" / "emea_observations_bpo_mapped.csv"
COORDS = ASSET_ROOT / "config" / "airport_coords.json"
ROUTE_COORDS = ASSET_ROOT / "config" / "airport_coords_routegen.json"
COMPETITOR = ASSET_ROOT / "config" / "airport_competitor_mapping.json"
CURRENT_VENDORED = ASSET_ROOT / "config" / "dashboard_baseline" / "current.csv"

TOL = 1e-6  # coverage/centroid fields are rounded in the JSON; tiny tol is slack


def _close(a, b, tol=TOL) -> bool:
    try:
        return abs(float(a) - float(b)) <= tol
    except (TypeError, ValueError):
        return a == b


def _cmp_route(g: dict, e: dict, where: str) -> list[str]:
    out = []
    # Exact fields.
    for k in ("rank", "hex_id", "child_hex_id", "dist_bucket",
              "n_sessions_in_parent", "n_sessions_in_child"):
        if g.get(k) != e.get(k):
            out.append(f"{where}.{k}: {g.get(k)!r} != {e.get(k)!r}")
    for scen in ("max", "mid", "light"):
        for k in (f"k_obs_{scen}", f"picked_buckets_{scen}", f"sample_hours_{scen}"):
            if g.get(k) != e.get(k):
                out.append(f"{where}.{k}: {g.get(k)!r} != {e.get(k)!r}")
    # Float fields.
    for k in ("avg_dist_km", "geo_share", "geo_cum"):
        if not _close(g.get(k), e.get(k)):
            out.append(f"{where}.{k}: {g.get(k)} != {e.get(k)}")
    for coord in ("lat", "lng"):
        if not _close((g.get("centroid") or {}).get(coord),
                      (e.get("centroid") or {}).get(coord)):
            out.append(f"{where}.centroid.{coord}: "
                       f"{(g.get('centroid') or {}).get(coord)} != "
                       f"{(e.get('centroid') or {}).get(coord)}")
    return out


def _cmp_routes_json(got: dict, exp: dict, apt: str) -> list[str]:
    out = []
    for k in ("algo_version", "n_time_buckets", "bucket_order", "scenarios",
              "time_bucket_mid_hour", "run_dt"):
        if got.get(k) != exp.get(k):
            out.append(f"{apt}: top.{k} differs")
    gt, et = got.get("trip_types", {}), exp.get("trip_types", {})
    if set(gt) != set(et):
        out.append(f"{apt}: trip_types {sorted(gt)} != {sorted(et)}")
    for trip in sorted(set(gt) & set(et)):
        G, E = gt[trip], et[trip]
        for k in ("n_max_routes", "n_total_routes", "total_sessions"):
            if G.get(k) != E.get(k):
                out.append(f"{apt}/{trip}.{k}: {G.get(k)} != {E.get(k)}")
        gr, er = G.get("ordered_routes", []), E.get("ordered_routes", [])
        if len(gr) != len(er):
            out.append(f"{apt}/{trip}: ordered_routes {len(gr)} != {len(er)}")
        for i in range(min(len(gr), len(er))):
            out += _cmp_route(gr[i], er[i], f"{apt}/{trip}.route[{i}]")
        # snapshots
        gs, es = G.get("snapshots", {}), E.get("snapshots", {})
        for scen in sorted(set(gs) | set(es)):
            a, b = gs.get(scen, []), es.get(scen, [])
            if len(a) != len(b):
                out.append(f"{apt}/{trip}.snap[{scen}]: {len(a)} != {len(b)}")
                continue
            for i in range(len(a)):
                if a[i]["n_routes"] != b[i]["n_routes"] or \
                        a[i]["n_observations"] != b[i]["n_observations"]:
                    out.append(f"{apt}/{trip}.snap[{scen}][{i}]: n_routes/n_obs differ")
                if not _close(a[i]["sample"], b[i]["sample"]) or \
                        not _close(a[i]["geo"], b[i]["geo"]):
                    out.append(f"{apt}/{trip}.snap[{scen}][{i}]: sample/geo differ")
                for arr in ("sb", "gb"):
                    if any(not _close(x, y) for x, y in zip(a[i][arr], b[i][arr])):
                        out.append(f"{apt}/{trip}.snap[{scen}][{i}].{arr} differ")
        # baseline
        gb, eb = G.get("baseline", {}), E.get("baseline", {})
        for k in ("n_today", "obs_today_wk", "obs_today_total", "domain"):
            if gb.get(k) != eb.get(k):
                out.append(f"{apt}/{trip}.baseline.{k}: {gb.get(k)} != {eb.get(k)}")
        for k in ("sample", "geo"):
            if not _close(gb.get(k), eb.get(k)):
                out.append(f"{apt}/{trip}.baseline.{k}: {gb.get(k)} != {eb.get(k)}")
    return out


def _cmp_lut(got: dict, exp: dict, apt: str) -> list[str]:
    out = []
    if set(got) != set(exp):
        out.append(f"{apt} LUT: trips {sorted(got)} != {sorted(exp)}")
    for trip in sorted(set(got) & set(exp)):
        G, E = got[trip], exp[trip]
        for section in ("sample_cell", "denom_s", "cell_hex_count", "denom_g"):
            gk, ek = G.get(section, {}), E.get(section, {})
            if set(gk) != set(ek):
                out.append(f"{apt}/{trip} LUT.{section}: key set differs "
                           f"({len(gk)} vs {len(ek)})")
            for key in set(gk) & set(ek):
                if not _close(gk[key], ek[key]):
                    out.append(f"{apt}/{trip} LUT.{section}[{key}]: {gk[key]} != {ek[key]}")
        if not _close(G.get("total"), E.get("total")):
            out.append(f"{apt}/{trip} LUT.total: {G.get('total')} != {E.get('total')}")
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--airports", default=None, help="comma-separated subset")
    ap.add_argument("--sample", type=int, default=0,
                    help="check only the first N vendored airports (sorted)")
    args = ap.parse_args(argv)

    vendored = sorted(p.name for p in VENDORED_ROUTES.iterdir() if p.is_dir())
    if args.airports:
        want = [a.strip().upper() for a in args.airports.split(",") if a.strip()]
    elif args.sample:
        want = vendored[:args.sample]
    else:
        want = vendored

    print(f"[routegen-parity] regenerating {len(want)} airport(s) from "
          f"{DEMAND_CSV.name} ...")
    tmp = Path(tempfile.mkdtemp(prefix="routegen_parity_"))
    build_routes.build(
        demand_csv=DEMAND_CSV, obs_csv=OBS_CSV, coords_path=COORDS,
        route_coords_path=ROUTE_COORDS, competitor_path=COMPETITOR,
        out_routes_dir=tmp / "routes",
        out_lut_dir=tmp / "lut", current_csv_path=tmp / "current.csv",
        airports=want, run_dt=date(2026, 5, 12))

    problems: list[str] = []
    n_routes_ok = n_lut_ok = 0
    for apt in want:
        gr = tmp / "routes" / apt / "routes.json"
        er = VENDORED_ROUTES / apt / "routes.json"
        if not er.exists():
            problems.append(f"{apt}: not in vendored routes ({er})")
            continue
        if not gr.exists():
            problems.append(f"{apt}: regeneration produced no routes.json")
            continue
        rp = _cmp_routes_json(json.loads(gr.read_text()),
                              json.loads(er.read_text()), apt)
        problems += rp
        if not rp:
            n_routes_ok += 1
        # LUT
        gl = tmp / "lut" / f"{apt}.json"
        el = VENDORED_LUT / f"{apt}.json"
        if el.exists() and gl.exists():
            lp = _cmp_lut(json.loads(gl.read_text()), json.loads(el.read_text()), apt)
            problems += lp
            if not lp:
                n_lut_ok += 1
        elif el.exists():
            problems.append(f"{apt}: regeneration produced no LUT")

    print(f"\n[routegen-parity] routes.json identical: {n_routes_ok}/{len(want)}")
    print(f"[routegen-parity] demand LUT identical:  {n_lut_ok}/{len(want)}")
    if problems:
        print(f"\n❌ {len(problems)} mismatch(es). First 40:")
        for p in problems[:40]:
            print("   -", p)
        return 1
    print("\n✅ ALL ROUTE-GEN PARITY CHECKS PASSED — the in-asset generator "
          "reproduces the vendored snapshot field-for-field.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
