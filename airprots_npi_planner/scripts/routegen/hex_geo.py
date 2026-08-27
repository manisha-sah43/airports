"""Hex-id -> (lat, lng) centroid resolution.

Strategy:
1. If the `h3` Python package is installed, use it (fast, deterministic).
2. Else, fall back to a Presto round-trip via `get_hexagon_addr_wkt()` and
   compute the polygon centroid client-side. Slower (one query per batch of
   unique hex ids) but works in environments without h3.

The fallback is used by `resolve_centroids(hex_ids, user_email=...)`.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable

log = logging.getLogger(__name__)

try:
    import h3                                                      # type: ignore
    _HAS_H3 = True
except Exception:                                                   # pragma: no cover
    _HAS_H3 = False


_WKT_POINT = re.compile(r"-?\d+(?:\.\d+)?")


def _h3_centroid(hex_id: str) -> tuple[float, float]:
    """Return (lat, lng) for an H3 cell using the local h3 package."""
    if hasattr(h3, "h3_to_geo"):                                    # h3 v3
        lat, lng = h3.h3_to_geo(hex_id)
    else:                                                            # h3 v4
        lat, lng = h3.cell_to_latlng(hex_id)
    return float(lat), float(lng)


def _h3_boundary(hex_id: str) -> list[tuple[float, float]]:
    """Return the polygon boundary of an H3 cell as a list of (lat, lng).

    Output is a closed-ring-friendly list (first vertex is NOT duplicated at
    the end — the consumer can close it as needed for Leaflet/GeoJSON). Works
    for any H3 resolution.
    """
    if hasattr(h3, "h3_to_geo_boundary"):                            # h3 v3
        verts = h3.h3_to_geo_boundary(hex_id)                       # [(lat, lng), ...]
    else:                                                            # h3 v4
        verts = h3.cell_to_boundary(hex_id)                         # [(lat, lng), ...]
    return [(float(lat), float(lng)) for lat, lng in verts]


def _wkt_centroid(wkt: str) -> tuple[float, float]:
    """Average all (x, y) coords in a WKT POLYGON string. Lng then lat in WKT."""
    pairs = _wkt_pairs(wkt)
    lng = sum(p[0] for p in pairs) / len(pairs)
    lat = sum(p[1] for p in pairs) / len(pairs)
    return lat, lng


def _wkt_boundary(wkt: str) -> list[tuple[float, float]]:
    """Extract all (lat, lng) vertices from a WKT POLYGON string.

    Returns the open ring (first vertex not duplicated at the end).
    """
    pairs = _wkt_pairs(wkt)
    return [(lat, lng) for lng, lat in pairs]


def _wkt_pairs(wkt: str) -> list[tuple[float, float]]:
    """Parse a WKT polygon into (lng, lat) pairs, dropping the closing duplicate."""
    nums = [float(s) for s in _WKT_POINT.findall(wkt)]
    if len(nums) < 6 or len(nums) % 2 != 0:
        raise ValueError(f"Cannot parse WKT pairs from: {wkt[:80]}…")
    pairs = list(zip(nums[0::2], nums[1::2]))
    if pairs and pairs[0] == pairs[-1] and len(pairs) > 1:
        pairs = pairs[:-1]
    return pairs


def _resolve_via_presto(hex_ids: list[str], user_email: str) -> dict[str, tuple[float, float]]:
    """Round-trip to Presto: get_hexagon_addr_wkt(hex_id) -> WKT, parse centroid."""
    from queryrunner_client import Client                            # lazy import

    if not hex_ids:
        return {}
    rows_clause = ", ".join("(" + repr(h) + ")" for h in hex_ids)
    sql = (
        "select hex_id, get_hexagon_addr_wkt(hex_id) as wkt "
        f"from (values {rows_clause}) as t(hex_id)"
    )
    log.info("Resolving %d hex centroids via Presto fallback …", len(hex_ids))
    client = Client(user_email=user_email)
    cursor = client.execute("presto", sql)
    df = cursor.to_pandas()
    out: dict[str, tuple[float, float]] = {}
    for _, row in df.iterrows():
        out[str(row["hex_id"])] = _wkt_centroid(row["wkt"])
    return out


def resolve_centroids(hex_ids: Iterable[str],
                      user_email: str | None = None) -> dict[str, tuple[float, float]]:
    """Map every unique hex_id to (lat, lng).

    Uses local h3 if available; otherwise falls back to a Presto round-trip
    (requires user_email).
    """
    uniq = sorted({str(h) for h in hex_ids if h})
    if not uniq:
        return {}
    if _HAS_H3:
        return {h: _h3_centroid(h) for h in uniq}
    if not user_email:
        raise RuntimeError(
            "h3 not installed and no user_email provided for the Presto fallback"
        )
    return _resolve_via_presto(uniq, user_email=user_email)


def cell_boundaries(hex_ids: Iterable[str],
                    user_email: str | None = None) -> dict[str, list[tuple[float, float]]]:
    """Map every unique hex_id to its polygon boundary as a list of (lat, lng).

    Mirrors `resolve_centroids`: local h3 if available, else Presto fallback.
    Output ring is open (first vertex not duplicated at the end). Works for
    any H3 resolution.
    """
    uniq = sorted({str(h) for h in hex_ids if h})
    if not uniq:
        return {}
    if _HAS_H3:
        return {h: _h3_boundary(h) for h in uniq}
    if not user_email:
        raise RuntimeError(
            "h3 not installed and no user_email provided for the Presto fallback"
        )
    return _resolve_boundaries_via_presto(uniq, user_email=user_email)


def _resolve_boundaries_via_presto(hex_ids: list[str],
                                   user_email: str) -> dict[str, list[tuple[float, float]]]:
    """Round-trip to Presto: get_hexagon_addr_wkt(hex_id) -> WKT, parse boundary."""
    from queryrunner_client import Client                            # lazy import

    if not hex_ids:
        return {}
    rows_clause = ", ".join("(" + repr(h) + ")" for h in hex_ids)
    sql = (
        "select hex_id, get_hexagon_addr_wkt(hex_id) as wkt "
        f"from (values {rows_clause}) as t(hex_id)"
    )
    log.info("Resolving %d hex boundaries via Presto fallback …", len(hex_ids))
    client = Client(user_email=user_email)
    cursor = client.execute("presto", sql)
    df = cursor.to_pandas()
    out: dict[str, list[tuple[float, float]]] = {}
    for _, row in df.iterrows():
        out[str(row["hex_id"])] = _wkt_boundary(row["wkt"])
    return out


# ── Polygon snap helpers ───────────────────────────────────────────────
# H3 child cells are not strictly contained in their parents at the cell
# boundary — a child's geometric centroid can sit just outside the parent's
# polygon. We snap such centroids to the nearest point on the parent boundary
# so route pins always land inside the hex they're attributed to.

def point_in_polygon(point: tuple[float, float],
                     polygon: list[tuple[float, float]]) -> bool:
    """Ray-cast point-in-polygon. Point and polygon vertices are (lat, lng)."""
    if not polygon:
        return False
    x, y = point[1], point[0]   # x=lng, y=lat
    inside = False
    n = len(polygon)
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i][1], polygon[i][0]
        xj, yj = polygon[j][1], polygon[j][0]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _closest_point_on_segment(p: tuple[float, float],
                              a: tuple[float, float],
                              b: tuple[float, float]) -> tuple[float, float]:
    """Closest point on segment a→b to p. All (lat, lng); planar approx."""
    ax, ay = a[1], a[0]   # x=lng, y=lat
    bx, by = b[1], b[0]
    px, py = p[1], p[0]
    dx, dy = bx - ax, by - ay
    if dx == 0.0 and dy == 0.0:
        return a
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return (ay + t * dy, ax + t * dx)   # back to (lat, lng)


def snap_to_polygon(point: tuple[float, float],
                    polygon: list[tuple[float, float]],
                    inset_eps: float = 5e-2) -> tuple[float, float]:
    """If point is outside polygon, return the closest point on its boundary
    nudged toward the polygon centroid so it sits visibly inside.

    `inset_eps` is the fraction of the boundary→centroid vector to move
    inward. Default 5e-2 ≈ 70 m at H3 res-7 scale — small enough that the
    pin still represents the dominant child cluster, large enough that on a
    city-zoom map the pin is clearly inside one parent rather than sitting
    on the shared border with a neighbour. If the point is already inside,
    it's returned unchanged. Empty polygon → point returned as-is. Treats
    lat/lng as planar — fine at H3 cell scale and well away from the poles.
    """
    if not polygon or point_in_polygon(point, polygon):
        return point
    best = point
    best_d2 = float("inf")
    n = len(polygon)
    for i in range(n):
        a = polygon[i]
        b = polygon[(i + 1) % n]
        c = _closest_point_on_segment(point, a, b)
        d2 = (point[0] - c[0]) ** 2 + (point[1] - c[1]) ** 2
        if d2 < best_d2:
            best_d2 = d2
            best = c
    if inset_eps > 0:
        cx = sum(v[0] for v in polygon) / n
        cy = sum(v[1] for v in polygon) / n
        best = (best[0] + inset_eps * (cx - best[0]),
                best[1] + inset_eps * (cy - best[1]))
    return best
