"""City-agnostic feature pools for editv2 miners.

Replaces the missing ``paris_pools`` module with a generic implementation that
any city can feed, as long as features are supplied as a JSONL of

    {"fid": "...", "kind": "building|green|road|point", "wkt": "...",
     "semantics": {...}}

in a **projected metric CRS** (metres).  ``localize`` recentres a group of
geometries on the local origin so published scenes carry small coordinates.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import geopandas as gpd
import pandas as pd
from shapely import affinity
from shapely import wkt as shapely_wkt
from shapely.geometry import box
from shapely.geometry.base import BaseGeometry

KINDS = ("building", "green", "road", "point")


# --------------------------------------------------------------------- geometry
def localize(geoms: Sequence[BaseGeometry]) -> Tuple[List[BaseGeometry], float, float]:
    """Translate ``geoms`` so their common bbox centre sits at the origin.

    Returns ``(local_geoms, ox, oy)`` with ``local = translate(g, -ox, -oy)``.
    """
    minx = min(g.bounds[0] for g in geoms)
    miny = min(g.bounds[1] for g in geoms)
    maxx = max(g.bounds[2] for g in geoms)
    maxy = max(g.bounds[3] for g in geoms)
    ox = (minx + maxx) / 2.0
    oy = (miny + maxy) / 2.0
    local = [affinity.translate(g, xoff=-ox, yoff=-oy) for g in geoms]
    return local, ox, oy


def feature_record(
    feature_id: str,
    geom: BaseGeometry,
    semantics: Optional[Dict[str, Any]],
    source_object_id: Any,
) -> Dict[str, Any]:
    """Scene-feature dict consumed by ``validate.evaluate_edit``."""
    c = geom.centroid
    return {
        "feature_id": feature_id,
        "geometry_type": geom.geom_type,
        "wkt": geom.wkt,
        "geojson": json.loads(gpd.GeoSeries([geom]).to_json())["features"][0]["geometry"],
        "bounding_box": [float(v) for v in geom.bounds],
        "centroid": [float(c.x), float(c.y)],
        "area": float(geom.area),
        "length": float(geom.length),
        "semantics": dict(semantics or {}),
        "source_object_id": source_object_id,
    }


# ------------------------------------------------------------------------ pools
def _gdf(rows: List[Dict[str, Any]]) -> gpd.GeoDataFrame:
    if not rows:
        return gpd.GeoDataFrame(
            {"fid": [], "kind": [], "semantics": []},
            geometry=gpd.GeoSeries([], dtype="geometry"),
        )
    return gpd.GeoDataFrame(
        {
            "fid": [r["fid"] for r in rows],
            "kind": [r["kind"] for r in rows],
            "semantics": [r.get("semantics") or {} for r in rows],
        },
        geometry=[r["geometry"] for r in rows],
    )


def load_pools(features_path: Path) -> Tuple[gpd.GeoDataFrame, ...]:
    """Return ``(buildings, green, roads, points)``."""
    buckets: Dict[str, List[Dict[str, Any]]] = {k: [] for k in KINDS}
    with open(features_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            kind = rec["kind"]
            if kind not in buckets:
                continue
            geom = shapely_wkt.loads(rec["wkt"])
            if geom.is_empty or not geom.is_valid:
                geom = geom.buffer(0) if geom.geom_type in ("Polygon", "MultiPolygon") else geom
                if geom.is_empty:
                    continue
            buckets[kind].append(
                {"fid": rec["fid"], "kind": kind, "semantics": rec.get("semantics") or {}, "geometry": geom}
            )
    return tuple(_gdf(buckets[k]) for k in KINDS)


def _cap(df: gpd.GeoDataFrame, n: int, rng: random.Random) -> gpd.GeoDataFrame:
    if n is None or len(df) <= n:
        return df.reset_index(drop=True)
    idx = sorted(rng.sample(range(len(df)), n))
    return df.iloc[idx].reset_index(drop=True)


def cap_pools(
    pools: Tuple[gpd.GeoDataFrame, ...],
    *,
    max_buildings: int = 8000,
    max_green: int = 600,
    max_roads: int = 2500,
    max_points: int = 20000,
    seed: int = 42,
) -> Tuple[gpd.GeoDataFrame, ...]:
    rng = random.Random(seed)
    bdf, gdf, rdf, pdf = pools
    return (
        _cap(bdf, max_buildings, rng),
        _cap(gdf, max_green, rng),
        _cap(rdf, max_roads, rng),
        _cap(pdf, max_points, rng),
    )


def clip_pools(pools: Tuple[gpd.GeoDataFrame, ...], bounds: Tuple[float, float, float, float]):
    win = box(*bounds)
    out = []
    for df in pools:
        if len(df) == 0:
            out.append(df)
            continue
        keep = df[df.geometry.intersects(win)].reset_index(drop=True)
        out.append(keep)
    return tuple(out)
