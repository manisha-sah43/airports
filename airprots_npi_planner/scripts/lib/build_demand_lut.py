"""build_demand_lut.py — standalone (re)generator of the per-airport demand LUTs.

Thin wrapper kept for backwards compatibility. The LUT build now lives in the
vendored route-generation package (`routegen`), and the normal way to (re)build
LUTs is Step 0 of the pipeline (`regenerate_routes` in inputs/config.json), which
produces routes.json and the LUTs from one consistent pass. This script lets a
maintainer rebuild ONLY the LUTs from the vendored demand CSV.

No longer imports `shared/` — the asset is fully self-contained.

Each output <AIRPORT>.json holds, per trip:
  { "PU": {sample_cell{"tb|db":sess}, denom_s{tb:sess}, cell_hex_count{"tb|db":n},
           denom_g{tb:n}, total}, "DO": {...} }
computed on the airport's demand cells with the airport parent-hex + 1-ring
excluded (mirrors how routes.json was built).

Usage (inside the asset .venv, which has pandas + h3):
  python build_demand_lut.py \
      --demand-csv ../../config/demand_2026-05-12/emea_demand_full.csv \
      --airport-coords ../../config/airport_coords.json \
      --out-dir ../../config/demand_lut_2026-05-12 \
      [--airports LHR,LGW,STN,LTN,JNB,WAW,CPT,CDG,ORY]   # default: all in demand
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

# Self-contained: import the vendored route-generation package (asset scripts/
# is on sys.path when run via the pipeline; add it when run standalone).
_SCRIPTS = Path(__file__).resolve().parents[1]
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
from routegen.bucket_alloc import _build_lookup_tables  # noqa: E402
from routegen.build_routes import _airport_ring_hexes, DEFAULT_HEX_SIZE  # noqa: E402


def _lut_to_json(cells: pd.DataFrame) -> dict:
    sample_cell, denom_s, cell_hex_count, denom_g, total = _build_lookup_tables(cells)
    return {
        "sample_cell": {f"{t}|{d}": v for (t, d), v in sample_cell.items()},
        "denom_s": {t: v for t, v in denom_s.items()},
        "cell_hex_count": {f"{t}|{d}": v for (t, d), v in cell_hex_count.items()},
        "denom_g": {t: v for t, v in denom_g.items()},
        "total": total,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--demand-csv", required=True, type=Path)
    ap.add_argument("--airport-coords", required=True, type=Path)
    ap.add_argument("--out-dir", required=True, type=Path)
    ap.add_argument("--airports", default=None,
                    help="comma-separated IATA subset; default = all in demand")
    args = ap.parse_args(argv)
    args.out_dir.mkdir(parents=True, exist_ok=True)

    coords = json.loads(args.airport_coords.read_text())
    demand = pd.read_csv(args.demand_csv)
    demand["airport_code"] = demand["airport_code"].astype(str)
    for c in ("trip_type", "hex_id", "time_bucket", "dist_bucket"):
        if c in demand.columns:
            demand[c] = demand[c].astype(str)

    wanted = ({a.strip().upper() for a in args.airports.split(",")}
              if args.airports else set(demand["airport_code"].str.upper().unique()))
    n = 0
    for apt in sorted(wanted):
        if apt not in coords:
            print(f"  skip {apt}: no coords", file=sys.stderr)
            continue
        d = demand[demand["airport_code"].str.upper() == apt]
        if d.empty:
            print(f"  skip {apt}: no demand", file=sys.stderr)
            continue
        excl = _airport_ring_hexes(apt, coords[apt], DEFAULT_HEX_SIZE)
        d = d[~d["hex_id"].isin(excl)]
        out = {}
        for trip in ("PU", "DO"):
            cells = d[d["trip_type"] == trip].copy()
            if not cells.empty:
                out[trip] = _lut_to_json(cells)
        (args.out_dir / f"{apt}.json").write_text(json.dumps(out))
        n += 1
        if n % 20 == 0:
            print(f"  {n} airports done...", file=sys.stderr)
    print(f"wrote {n} LUT files to {args.out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
