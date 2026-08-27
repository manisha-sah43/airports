"""recompute_realized_quality.py — realized NPI quality after the 30/hr trim.

The planned tiers come from the routes.json snapshot at N. The 30-obs/hour BPO
cap forces `schedule_shifts.py` to drop the least-important cells; realized
quality = planned minus exactly the coverage the dropped cells contributed.

Adapted from projects/2026-05-29_.../scripts/recompute_realized_quality.py.
The original replays coverage over the 206 MB demand table; this asset instead
reads a tiny PRECOMPUTED per-airport lookup table (config/demand_lut_<date>/
<AIRPORT>.json), so the computation is exact but demand-free at runtime. Those
LUTs are the `_build_lookup_tables` output; regenerate with lib/build_demand_lut.py.

Fast path: a cut with ZERO dropped rows has realized == planned and needs no LUT.
Only trimmed cuts consult the LUT. If a trimmed cut has no LUT, realized is
reported == planned with status `trim_delta_unavailable` (flagged, never silent).
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from lib.curve_config import BUCKET_ORDER
from lib.tiering import snapshot_at, quality_from_snapshot

OUT_COLUMNS = (
    "airport_code", "trip_type", "n_dropped",
    "planned_NPI_quality", "realized_NPI_quality",
    "planned_time_NPI_quality", "realized_time_NPI_quality",
    "planned_sample_coverage", "realized_sample_coverage",
    "planned_g_coverage", "realized_g_coverage",
    "tier_changed", "status",
)


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, round(x, 4)))


def _load_lut(path: Path) -> dict | None:
    """Return {trip: parsed_lut} for the airport, or None if the file is missing.
    On disk each trip maps to {sample_cell{"tb|db":v}, denom_s, cell_hex_count,
    denom_g, total}; we restore the tuple keys here."""
    if not path.exists():
        return None
    raw = json.loads(path.read_text())

    def _parse(trip_lut: dict) -> dict:
        return {
            "sample_cell": {tuple(k.split("|", 1)): float(v)
                            for k, v in trip_lut["sample_cell"].items()},
            "denom_s": {k: float(v) for k, v in trip_lut["denom_s"].items()},
            "cell_hex_count": {tuple(k.split("|", 1)): int(v)
                               for k, v in trip_lut["cell_hex_count"].items()},
            "denom_g": {k: int(v) for k, v in trip_lut["denom_g"].items()},
            "total": float(trip_lut["total"]),
        }
    return {trip: _parse(tl) for trip, tl in raw.items()}


def _coverage(ordered: list[dict], lut: dict, scen: str, n: int,
              drop: set[tuple[int, str]]) -> dict:
    """Replay build_snapshots accumulation over ordered_routes[:n], skipping any
    (rank, bucket) in `drop`. Uses the precomputed LUT instead of demand."""
    sample_cell = lut["sample_cell"]
    denom_s = lut["denom_s"]
    cell_hex_count = lut["cell_hex_count"]
    denom_g = lut["denom_g"]
    total = lut["total"]
    sample_w = {k: v / total for k, v in sample_cell.items()} if total > 0 else {}
    covered: set[tuple[str, str]] = set()
    cs = {t: 0.0 for t in BUCKET_ORDER}
    cg = {t: 0 for t in BUCKET_ORDER}
    sample_cov = geo_cov = 0.0
    pb = f"picked_buckets_{scen}"
    for i, r in enumerate(ordered, start=1):
        if i > n:
            break
        dh = str(r["dist_bucket"])
        picks = [tb for tb in (r.get(pb) or []) if (i, tb) not in drop]
        if picks:
            geo_cov = min(geo_cov + float(r["geo_share"]), 1.0)
        for tb in picks:
            cell = (tb, dh)
            if cell in covered:
                continue
            covered.add(cell)
            sample_cov = min(sample_cov + sample_w.get(cell, 0.0), 1.0)
            cs[tb] += sample_cell.get(cell, 0.0)
            cg[tb] += cell_hex_count.get(cell, 0)
    sb = [round((cs[t] / denom_s[t]) if denom_s.get(t, 0) > 0 else 0.0, 4)
          for t in BUCKET_ORDER]
    gb = [round((cg[t] / denom_g[t]) if denom_g.get(t, 0) > 0 else 0.0, 4)
          for t in BUCKET_ORDER]
    return {"sample": round(sample_cov, 4), "geo": round(geo_cov, 4),
            "sb": sb, "gb": gb}


def recompute(cuts: list[dict], routes_dir: Path, lut_dir: Path,
              dropped_csv: Path, out_path: Path) -> list[dict]:
    # dropped picks keyed (airport, trip) -> {(rank, bucket)}
    drops: dict[tuple[str, str], set] = {}
    if dropped_csv.exists():
        for r in csv.DictReader(dropped_csv.open()):
            drops.setdefault((r["airport"].upper(), r["trip"].upper()), set()).add(
                (int(r["route_rank"]), r["time_bucket"]))

    out_rows = []
    for cut in cuts:
        apt = cut["airport"].upper()
        trip = cut["trip_type"].upper()
        scen = cut["scenario"].lower()
        n = int(cut["n_routes"])
        block = json.loads((routes_dir / apt / "routes.json").read_text())["trip_types"][trip]
        pub = snapshot_at((block.get("snapshots", {}) or {}).get(scen, []), n)
        if not pub:
            continue
        ps, pg, pNPI, ptime = quality_from_snapshot(pub)
        drop = drops.get((apt, trip), set())

        if not drop:
            realized, status = pub, "no_trim"
        else:
            lut = _load_lut(lut_dir / f"{apt}.json")
            lut_trip = lut.get(trip) if lut else None
            if not lut_trip:
                realized, status = pub, "trim_delta_unavailable"
            else:
                ordered = block["ordered_routes"]
                full = _coverage(ordered, lut_trip, scen, n, set())
                trim = _coverage(ordered, lut_trip, scen, n, drop)
                realized = {
                    "sample": _clamp(pub["sample"] + (trim["sample"] - full["sample"])),
                    "geo": _clamp(pub["geo"] + (trim["geo"] - full["geo"])),
                    "sb": [_clamp(pub["sb"][i] + (trim["sb"][i] - full["sb"][i]))
                           for i in range(len(BUCKET_ORDER))],
                    "gb": [_clamp(pub["gb"][i] + (trim["gb"][i] - full["gb"][i]))
                           for i in range(len(BUCKET_ORDER))],
                }
                status = "trim_applied"

        rs, rg, rNPI, rtime = quality_from_snapshot(realized)
        out_rows.append({
            "airport_code": apt, "trip_type": trip, "n_dropped": len(drop),
            "planned_NPI_quality": pNPI, "realized_NPI_quality": rNPI,
            "planned_time_NPI_quality": ptime, "realized_time_NPI_quality": rtime,
            "planned_sample_coverage": round(pub["sample"], 4),
            "realized_sample_coverage": round(realized["sample"], 4),
            "planned_g_coverage": round(pub["geo"], 4),
            "realized_g_coverage": round(realized["geo"], 4),
            "tier_changed": "YES" if (pNPI, ptime) != (rNPI, rtime) else "no",
            "status": status,
        })

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(OUT_COLUMNS))
        w.writeheader()
        w.writerows(out_rows)
    return out_rows
