#!/usr/bin/env python3
"""Build the formal v5 non-topology L1 set from the inherited v3.9 registry.

The build intentionally keeps the inherited 32-relation registry immutable and
emits a separate 28-relation active registry plus a four-relation deferred list.
Normal use is through ``build_city_case.py``, which injects the configured
projected CRS, city and source provenance. Transparent constructions are marked
in private data.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import random
from collections import Counter, defaultdict
from pathlib import Path

import geopandas as gpd
import shapely
from shapely import affinity
from shapely.geometry import LineString, Point, Polygon, mapping
from shapely.ops import substring
from shapely.wkt import loads as load_wkt


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SOURCE = Path("CONFIG_REQUIRED_USE_build_city_case.py")
DEFAULT_GPKG = Path("CONFIG_REQUIRED_USE_build_city_case.py")
ACTIVE_IDS = [
    "T01", "T02", "T03", "T04", "T05", "T06", "T08", "T09", "T11",
    "D01", "D02", "D04", "D05", "D06",
    "M01", "M02", "M04", "M08", "M12", "M14", "M15", "M18",
    "O01", "O02", "O03", "O04", "O06", "O08",
]
NON_TOPO_IDS = [x for x in ACTIVE_IDS if not x.startswith("T")]
DEFERRED = {
    "D03": "Sixteen-direction classification is uncommon and was removed by manual review.",
    "D07": "The inherited implementation duplicated eight-direction bearing rather than a distinct projection relation.",
    "O07": "Containment transitivity is deferred from the first formal L1 release.",
    "O09": "Scene editing belongs to the later transformation/reasoning layer, not this L1 release.",
}
MODES = [
    "wkt_exact",
    "geojson_centroid_bbox",
    "geojson_exact",
    "geojson_exact_attributes",
]
RNG = random.Random(20260818)


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(x, ensure_ascii=False, separators=(",", ":")) + "\n" for x in rows), encoding="utf-8")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def bearing(a: Point, b: Point) -> float:
    return (math.degrees(math.atan2(b.x - a.x, b.y - a.y)) + 360.0) % 360.0


def sector4(angle: float) -> str:
    return ["N", "E", "S", "W"][int(((angle + 45.0) % 360.0) // 90.0)]


def sector8(angle: float) -> str:
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int(((angle + 22.5) % 360.0) // 45.0)]


def axis4(angle: float) -> str:
    a = angle % 180.0
    if a < 22.5 or a >= 157.5:
        return "N-S"
    if a < 67.5:
        return "NE-SW"
    if a < 112.5:
        return "E-W"
    return "NW-SE"


def attrs_clean(attrs: dict | None) -> dict:
    keep = ("fclass", "name", "type", "oneway", "maxspeed", "layer", "bridge", "tunnel")
    return {k: attrs[k] for k in keep if attrs and k in attrs and attrs[k] not in (None, "")}


def obj(fid: str, role: str, geom, source: dict | None = None, derivation: str | None = None,
        attrs: dict | None = None) -> dict:
    src = source or {}
    return {
        "id": fid,
        "role": role,
        "geometry_type": geom.geom_type,
        "geometry_wkt": shapely.to_wkt(geom, rounding_precision=6),
        "source_id": src.get("source_id", "transparent-construction"),
        "source_layer": src.get("source_layer"),
        "source_attributes": attrs_clean(attrs if attrs is not None else src.get("attributes")),
        "derivation": derivation,
    }


def geom(o: dict):
    return load_wkt(o["geometry_wkt"])


def from_source(o: dict, fid: str, role: str | None = None) -> dict:
    return obj(fid, role or o.get("role", "object"), load_wkt(o["geometry_wkt"]), o)


def make_item(rid: str, idx: int, objects: list[dict], task: str, answer_format: str,
              gold_answer, oracle: dict, scoring: dict, construction: str = "observed",
              sufficiency: dict | None = None) -> dict:
    return {
        "item_id": f"STO-L1-{rid}-{idx:02d}",
        "relation_id": rid,
        "city": "Stockholm",
        "crs": "EPSG:3006",
        "objects": objects,
        "task": task,
        "answer_format": answer_format,
        "gold": {"answer": gold_answer, "oracle": oracle, "scoring": scoring},
        "provenance": {
            "dataset": "Geofabrik Sweden OSM 2026-07-20; central Stockholm clip",
            "construction": construction,
        },
        "mode_information_sufficient": sufficiency or {m: (m != "geojson_centroid_bbox") for m in MODES},
    }


def select_point_pairs(point_pool: list[dict], targets: list[str], eight: bool = False) -> list[tuple[dict, dict]]:
    pairs = []
    used = set()
    classify = (lambda x: sector8(x)) if eight else (lambda x: sector4(x))
    for target in targets:
        choices = []
        for i, a in enumerate(point_pool):
            for j, b in enumerate(point_pool):
                if i == j or (i, j) in used:
                    continue
                pa, pb = load_wkt(a["geometry_wkt"]), load_wkt(b["geometry_wkt"])
                ang = bearing(pa, pb)
                if classify(ang) == target:
                    margin = min(abs(((ang - q + 180) % 360) - 180) for q in ([45, 135, 225, 315] if not eight else [22.5 + 45*k for k in range(8)]))
                    choices.append((margin, i, j, a, b))
        if not choices:
            raise RuntimeError(f"No point pair for {target}")
        _, i, j, a, b = max(choices, key=lambda x: x[0])
        used.add((i, j))
        pairs.append((a, b))
    return pairs


def source_pools(cases: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    unique = {}
    for row in cases:
        for o in row.get("objects", []):
            if o.get("source_id") and o.get("derivation") is None:
                unique.setdefault((o["source_id"], o["geometry_wkt"]), o)
    vals = list(unique.values())
    points = [x for x in vals if load_wkt(x["geometry_wkt"]).geom_type == "Point"]
    lines = [x for x in vals if "LineString" in load_wkt(x["geometry_wkt"]).geom_type]
    polys = [x for x in vals if "Polygon" in load_wkt(x["geometry_wkt"]).geom_type]
    return points, lines, polys


def add_direction(items: list[dict], points: list[dict], lines: list[dict]) -> None:
    for idx, (a, b) in enumerate(select_point_pairs(points, ["N", "E", "S"]), 1):
        pa, pb = load_wkt(a["geometry_wkt"]), load_wkt(b["geometry_wkt"])
        ang = bearing(pa, pb)
        items.append(make_item("D01", idx, [from_source(a, "F01", "reference"), from_source(b, "F02", "target")],
            "Which four-direction class describes F02 relative to F01? Use N, E, S, or W; sector centres are 0°, 90°, 180°, and 270°.",
            "one token: N | E | S | W", sector4(ang), {"bearing_deg": round(ang, 6), "sector_rule": "nearest cardinal centre"}, {"type": "exact"}))
    for idx, (a, b) in enumerate(select_point_pairs(points, ["NE", "SE", "W"], eight=True), 1):
        pa, pb = load_wkt(a["geometry_wkt"]), load_wkt(b["geometry_wkt"])
        ang = bearing(pa, pb)
        items.append(make_item("D02", idx, [from_source(a, "F01", "reference"), from_source(b, "F02", "target")],
            "Which eight-direction class describes F02 relative to F01? Use N, NE, E, SE, S, SW, W, or NW.",
            "one direction token", sector8(ang), {"bearing_deg": round(ang, 6), "sector_width_deg": 45}, {"type": "exact"}))
    for idx, (a, b) in enumerate(select_point_pairs(points, ["N", "SE", "W"], eight=True), 1):
        pa, pb = load_wkt(a["geometry_wkt"]), load_wkt(b["geometry_wkt"])
        ang = bearing(pa, pb)
        items.append(make_item("D04", idx, [from_source(a, "F01", "reference"), from_source(b, "F02", "target")],
            "What is the bearing from F01 to F02, measured clockwise from north?",
            "a number in degrees in [0,360)", round(ang, 2), {"bearing_deg": ang}, {"type": "circular_numeric", "absolute_tolerance_deg": 1.0}))
    selected = lines[:3]
    for idx, (line_src, target_src) in enumerate(zip(selected, points[3:6]), 1):
        line = load_wkt(line_src["geometry_wkt"])
        if line.geom_type == "MultiLineString":
            line = max(line.geoms, key=lambda g: g.length)
        start, end = Point(line.coords[0]), Point(line.coords[-1])
        target = load_wkt(target_src["geometry_wkt"])
        h, t = bearing(start, end), bearing(start, target)
        rel = ((t - h + 180) % 360) - 180
        if abs(rel) <= 45:
            label = "front"
        elif 45 < rel <= 135:
            label = "right"
        elif -135 <= rel < -45:
            label = "left"
        else:
            label = "back"
        items.append(make_item("D05", idx, [obj("F01", "directed_road", line, line_src), from_source(target_src, "F02", "target")],
            "Stand at the first stored coordinate of road F01 and face toward its last stored coordinate. Is F02 in front, right, back, or left?",
            "one token: front | right | back | left", label,
            {"road_heading_deg": h, "target_bearing_deg": t, "relative_angle_deg": rel}, {"type": "exact"},
            sufficiency={"wkt_exact": True, "geojson_centroid_bbox": False, "geojson_exact": True, "geojson_exact_attributes": True}))
    axis_sources = []
    for wanted in ("N-S", "NE-SW", "E-W"):
        for src in lines:
            line = load_wkt(src["geometry_wkt"])
            if line.geom_type == "MultiLineString": line = max(line.geoms, key=lambda g: g.length)
            if len(line.coords) >= 2 and axis4(bearing(Point(line.coords[0]), Point(line.coords[-1]))) == wanted and src not in axis_sources:
                axis_sources.append(src); break
    for idx, src in enumerate(axis_sources, 1):
        line = load_wkt(src["geometry_wkt"])
        if line.geom_type == "MultiLineString":
            line = max(line.geoms, key=lambda g: g.length)
        ang = bearing(Point(line.coords[0]), Point(line.coords[-1]))
        items.append(make_item("D06", idx, [obj("F01", "road", line, src)],
            "What is the undirected principal orientation of F01, based on its first-to-last coordinate chord?",
            "one token: N-S | NE-SW | E-W | NW-SE", axis4(ang), {"directed_bearing_deg": ang, "orientation_axis": axis4(ang)}, {"type": "exact"},
            sufficiency={"wkt_exact": True, "geojson_centroid_bbox": False, "geojson_exact": True, "geojson_exact_attributes": True}))


def add_metrics(items: list[dict], cases_by_id: dict[str, list[dict]], points: list[dict], lines: list[dict], polys: list[dict], pois: gpd.GeoDataFrame) -> None:
    for idx, row in enumerate(cases_by_id["M01"], 1):
        os = [from_source(row["objects"][0], "F01"), from_source(row["objects"][1], "F02")]
        d = geom(os[0]).distance(geom(os[1]))
        items.append(make_item("M01", idx, os, "What is the planar Euclidean distance between F01 and F02?", "a number in metres", round(d, 2), {"distance_m": d}, {"type": "numeric", "absolute_tolerance_m": 1.0}, sufficiency={m: True for m in MODES}))
    for idx, row in enumerate(cases_by_id["M02"], 1):
        os = [from_source(row["objects"][0], "F01"), from_source(row["objects"][1], "F02")]
        assert "Polygon" in geom(os[0]).geom_type and "Polygon" in geom(os[1]).geom_type
        d = geom(os[0]).boundary.distance(geom(os[1]).boundary)
        items.append(make_item("M02", idx, os, "What is the shortest boundary-to-boundary distance between polygon F01 and polygon F02?", "a number in metres", round(d, 2), {"boundary_distance_m": d}, {"type": "numeric", "absolute_tolerance_m": 1.0}))
    for idx, row in enumerate(cases_by_id["M04"], 1):
        os = [from_source(row["objects"][0], "F01"), from_source(row["objects"][1], "F02")]
        a, b = geom(os[0]), geom(os[1]); d = abs(a.x-b.x) + abs(a.y-b.y)
        items.append(make_item("M04", idx, os, "What is the Manhattan distance |Δx|+|Δy| between point F01 and point F02?", "a number in metres", round(d, 2), {"manhattan_distance_m": d}, {"type": "numeric", "absolute_tolerance_m": 1.0}, sufficiency={m: True for m in MODES}))
    for idx, row in enumerate(cases_by_id["M08"], 1):
        os = [from_source(row["objects"][0], "F01"), from_source(row["objects"][1], "F02")]
        d = float(shapely.hausdorff_distance(geom(os[0]), geom(os[1]), densify=0.25))
        items.append(make_item("M08", idx, os,
            "What is the symmetric densified discrete Hausdorff distance between F01 and F02? Use densify=0.25.",
            "a number in metres", round(d, 2), {"hausdorff_distance_m": d, "densify": 0.25, "symmetric": True}, {"type": "numeric", "absolute_tolerance_m": 1.0}))
    base_polys = [load_wkt(x["objects"][0]["geometry_wkt"]) for x in cases_by_id["M12"]]
    # Case 3 introduces a transparent hole so the convention is actually tested.
    p = max(base_polys[2].geoms, key=lambda g: g.area) if base_polys[2].geom_type == "MultiPolygon" else base_polys[2]
    inner = affinity.scale(p, xfact=0.35, yfact=0.35, origin="centroid")
    base_polys[2] = p.difference(inner)
    for idx, (row, p) in enumerate(zip(cases_by_id["M12"], base_polys), 1):
        deriv = "real OSM polygon with a transparent scaled interior ring added" if idx == 3 else None
        o = obj("F01", "polygon", p, row["objects"][0], derivation=deriv)
        total = p.boundary.length
        items.append(make_item("M12", idx, [o],
            "What is the total boundary length of polygon F01? Include every exterior ring and every interior-hole ring.",
            "a number in metres", round(total, 2), {"total_boundary_length_m": total, "interior_ring_count": sum(len(g.interiors) for g in p.geoms) if p.geom_type == "MultiPolygon" else len(p.interiors)}, {"type": "numeric", "absolute_tolerance_m": 1.0}, construction="transparent construction" if idx == 3 else "observed"))
    def nearest_poi_source(g, minimum_distance=1.0):
        ranked = pois.assign(_distance=pois.geometry.map(g.distance))
        rec = ranked[ranked._distance > minimum_distance].nsmallest(1, "_distance").iloc[0]
        src = {"source_id": f"gis_osm_pois_free:{rec.get('osm_id')}", "source_layer": "gis_osm_pois_free", "attributes": rec.to_dict(), "geometry_wkt": shapely.to_wkt(rec.geometry)}
        return src
    old_pair = cases_by_id["M14"][0]["objects"]
    m14_specs = [
        (old_pair[0], old_pair[1], 0.90, "Point-Point"),
        (lines[0], nearest_poi_source(load_wkt(lines[0]["geometry_wkt"])), 1.20, "LineString-Point"),
        (polys[0], nearest_poi_source(load_wkt(polys[0]["geometry_wkt"])), 0.90, "Polygon-Point"),
    ]
    for idx, (a_src, b_src, ratio, sig) in enumerate(m14_specs, 1):
        a, b = load_wkt(a_src["geometry_wkt"]), load_wkt(b_src["geometry_wkt"])
        d = a.distance(b); r = max(5.0, round(d * ratio / 5.0) * 5.0)
        ans = d <= r + 1e-9
        os = [from_source(a_src, "F01"), from_source(b_src, "F02")]
        items.append(make_item("M14", idx, os,
            f"Does F02 intersect the closed {r:g}-metre buffer of F01? (Equivalently, is the minimum geometry distance no greater than {r:g} m?)",
            "yes or no", "yes" if ans else "no", {"distance_m": d, "buffer_radius_m": r, "geometry_signature": sig, "closed_buffer": True}, {"type": "exact"}))
    for idx, row in enumerate(cases_by_id["M15"], 1):
        ref = row["objects"][0]
        cand = row["objects"][1:5]
        ref_geom = load_wkt(ref["geometry_wkt"])
        correct_source_i = min(range(4), key=lambda i: ref_geom.distance(load_wkt(cand[i]["geometry_wkt"])))
        target_label_index = (0, 1, 3)[idx-1]
        others = [i for i in range(4) if i != correct_source_i]
        perm = others[:target_label_index] + [correct_source_i] + others[target_label_index:]
        os = [from_source(ref, "F00", "reference")]
        for label_i, source_i in enumerate(perm, 1):
            os.append(from_source(cand[source_i], f"F{label_i:02d}", "candidate"))
        distances = {o["id"]: geom(os[0]).distance(geom(o)) for o in os[1:]}
        nearest = min(distances, key=distances.get)
        ordered = sorted(distances.values())
        items.append(make_item("M15", idx, os,
            "Which one of the four candidates F01, F02, F03, and F04 has the smallest planar distance to reference F00?",
            "one candidate ID", nearest, {"distance_by_candidate_m": distances, "nearest_margin_m": ordered[1]-ordered[0]}, {"type": "exact"}, sufficiency={m: True for m in MODES}))
    # M18: the set is exactly the explicitly supplied POIs, never an implicit city-wide set.
    for idx, row in enumerate(cases_by_id["M18"], 1):
        region_src = row["objects"][0]; region = load_wkt(region_src["geometry_wkt"])
        inside = pois[pois.geometry.map(region.covers)].copy()
        outside = pois[~pois.geometry.map(region.covers)].copy()
        outside["_d"] = outside.geometry.map(region.distance)
        inside_limit = (3, 5, 6)[idx-1]
        chosen_inside = list(inside.head(inside_limit).itertuples())
        chosen = chosen_inside + list(outside.nsmallest(10-len(chosen_inside), "_d").itertuples())
        os = [from_source(region_src, "F00", "region")]
        for j, rec in enumerate(chosen, 1):
            ad = {k: getattr(rec, k) for k in ("fclass", "name") if hasattr(rec, k)}
            src = {"source_id": f"gis_osm_pois_free:{getattr(rec, 'osm_id', j)}", "source_layer": "gis_osm_pois_free", "attributes": ad}
            os.append(obj(f"P{j:02d}", "explicit_poi", rec.geometry, src))
        count = sum(region.covers(geom(o)) for o in os[1:]); area = region.area / 1e6; density = count / area
        items.append(make_item("M18", idx, os,
            "Using only the explicitly supplied POIs P01 through P10, what is their density covered by region F00? Count boundary POIs as inside and divide by F00 area in km².",
            "a number in POIs/km²", round(density, 3), {"explicit_poi_count": len(os)-1, "covered_poi_count": count, "area_km2": area, "density_per_km2": density}, {"type": "numeric", "absolute_tolerance": 0.01}, sufficiency={"wkt_exact": True, "geojson_centroid_bbox": False, "geojson_exact": True, "geojson_exact_attributes": True}))


def straight_chord(line_src: dict) -> tuple[LineString, dict]:
    g = load_wkt(line_src["geometry_wkt"])
    if g.geom_type == "MultiLineString": g = max(g.geoms, key=lambda z: z.length)
    return LineString([g.coords[0], g.coords[-1]]), line_src


def offset_point(line: LineString, frac: float, side: float, dist: float) -> Point:
    p = line.interpolate(frac, normalized=True)
    a, b = Point(line.coords[0]), Point(line.coords[-1])
    dx, dy = b.x-a.x, b.y-a.y; n = math.hypot(dx, dy)
    return Point(p.x - side*dy/n*dist, p.y + side*dx/n*dist)


def add_order(items: list[dict], lines: list[dict], roads: gpd.GeoDataFrame) -> None:
    base = lines[:3]
    perms = [("F03", "F01", "F02"), ("F02", "F03", "F01"), ("F01", "F03", "F02")]
    for idx, (src, labels) in enumerate(zip(base, perms), 1):
        g = load_wkt(src["geometry_wkt"])
        if g.geom_type == "MultiLineString": g = max(g.geoms, key=lambda z: z.length)
        os = [obj("F00", "route", g, src)]
        fractions = [0.2, 0.5, 0.8]
        for label, frac in zip(labels, fractions):
            os.append(obj(label, "position", g.interpolate(frac, normalized=True), src, f"point sampled at normalized route measure {frac}"))
        items.append(make_item("O01", idx, os,
            "Along route F00 in its stored coordinate direction, order F01, F02, and F03 from first to last.",
            "three IDs joined by >", ">".join(labels), {"normalized_measure_by_id": dict(zip(labels, fractions))}, {"type": "ordered_ids"}, construction="points transparently sampled on a real OSM road", sufficiency={"wkt_exact": True, "geojson_centroid_bbox": False, "geojson_exact": True, "geojson_exact_attributes": True}))
    for idx, src in enumerate(base, 1):
        g = load_wkt(src["geometry_wkt"])
        if g.geom_type == "MultiLineString": g = max(g.geoms, key=lambda z: z.length)
        flip = idx % 2 == 0
        first, second = (("F02", 0.25), ("F01", 0.75)) if flip else (("F01", 0.25), ("F02", 0.75))
        os = [obj("F00", "route", g, src), obj(first[0], "position", g.interpolate(first[1], normalized=True), src, f"sampled at {first[1]}"), obj(second[0], "position", g.interpolate(second[1], normalized=True), src, f"sampled at {second[1]}")]
        items.append(make_item("O02", idx, os,
            "Along route F00 in its stored coordinate direction, which point is before the other?",
            "before=<ID>; after=<ID>", f"before={first[0]}; after={second[0]}", {"normalized_measure_by_id": {first[0]: first[1], second[0]: second[1]}}, {"type": "pairwise_before_after"}, construction="points transparently sampled on a real OSM road", sufficiency={"wkt_exact": True, "geojson_centroid_bbox": False, "geojson_exact": True, "geojson_exact_attributes": True}))
    side_specs = [(1, 1), (1, -1), (-1, -1)]
    for idx, (src, sides) in enumerate(zip(base, side_specs), 1):
        chord, src = straight_chord(src)
        a, b = offset_point(chord, .35, sides[0], 40), offset_point(chord, .70, sides[1], 55)
        relation = "same_side" if sides[0] == sides[1] else "opposite_sides"
        os = [obj("F00", "straight_reference_line", chord, src, "first-to-last chord of real road"), obj("F01", "point", a, src, "offset from real road chord"), obj("F02", "point", b, src, "offset from real road chord")]
        items.append(make_item("O03", idx, os,
            "Relative to the directed straight line F00 (first coordinate to last), are F01 and F02 on the same side or on opposite sides?",
            "same_side or opposite_sides", relation, {"cross_product_signs": list(sides)}, {"type": "exact"}, construction="straight chord and transparent offsets from a real OSM road", sufficiency={"wkt_exact": True, "geojson_centroid_bbox": False, "geojson_exact": True, "geojson_exact_attributes": True}))
    between_specs = [(0.5, 0.0), (1.2, 0.0), (0.6, 1.0)]
    for idx, (src, (t, off)) in enumerate(zip(base, between_specs), 1):
        chord, src = straight_chord(src); a, c = Point(chord.coords[0]), Point(chord.coords[-1])
        b = offset_point(chord, t, 1, off) if off else Point(a.x+t*(c.x-a.x), a.y+t*(c.y-a.y))
        vx, vy = c.x-a.x, c.y-a.y; wx, wy = b.x-a.x, b.y-a.y
        projected = (wx*vx+wy*vy)/(vx*vx+vy*vy)
        support_distance = abs(vx*wy-vy*wx)/math.hypot(vx, vy)
        ans = support_distance <= 1e-6 and 0 < projected < 1
        os = [obj("F01", "endpoint", a, src, "road chord start"), obj("F02", "candidate_between", b, src, "derived from road chord"), obj("F03", "endpoint", c, src, "road chord end")]
        items.append(make_item("O04", idx, os,
            "In the ordinary three-point collinear sense, is F02 strictly between F01 and F03?",
            "yes or no", "yes" if ans else "no", {"distance_to_supporting_line_m": support_distance, "projection_parameter_unclamped": projected, "strict_between": ans, "collinearity_tolerance_m": 1e-6}, {"type": "exact"}, construction="three points transparently derived from a real OSM road chord", sufficiency={m: True for m in MODES}))
    add_o06(items, roads)
    add_o08(items, base, roads)


def add_o06(items: list[dict], roads: gpd.GeoDataFrame) -> None:
    r = roads.copy()
    r["layer"] = r["layer"].fillna(0).astype(float)
    candidates = r[(r.layer != 0) | (r.bridge == "T") | (r.tunnel == "T")]
    diff, same = [], []
    seen = set()
    for i, row in candidates.iterrows():
        for j in r.sindex.query(row.geometry, predicate="intersects"):
            if i == j or tuple(sorted((int(i), int(j)))) in seen: continue
            seen.add(tuple(sorted((int(i), int(j)))))
            other = r.loc[j]
            rec = (row, other)
            (diff if row.layer != other.layer else same).append(rec)
        if len(diff) >= 2 and len(same) >= 1: break
    chosen = diff[:2] + same[:1]
    if len(chosen) != 3: raise RuntimeError("Could not find three local O06 road pairs")
    for idx, (a, b) in enumerate(chosen, 1):
        if idx == 2:
            a, b = b, a
        src_a = {"source_id": f"gis_osm_roads_free:{a.osm_id}", "source_layer": "gis_osm_roads_free", "attributes": a.to_dict()}
        src_b = {"source_id": f"gis_osm_roads_free:{b.osm_id}", "source_layer": "gis_osm_roads_free", "attributes": b.to_dict()}
        la, lb = float(a.layer), float(b.layer)
        ans = "same_layer" if la == lb else "F01" if la > lb else "F02"
        os = [obj("F01", "road", a.geometry, src_a), obj("F02", "road", b.geometry, src_b)]
        items.append(make_item("O06", idx, os,
            "At their 2D overlap/crossing, which road is vertically above according to the supplied OSM layer values, or are they at the same layer? Layer is ordinal stacking, not elevation in metres.",
            "F01 | F02 | same_layer", ans, {"F01_layer": la, "F02_layer": lb, "intersects_2d": a.geometry.intersects(b.geometry)}, {"type": "exact"}, sufficiency={"wkt_exact": False, "geojson_centroid_bbox": False, "geojson_exact": False, "geojson_exact_attributes": True}))


def split_chain(src: dict, n: int, prefix: str) -> tuple[list[dict], list[str]]:
    g = load_wkt(src["geometry_wkt"])
    if g.geom_type == "MultiLineString": g = max(g.geoms, key=lambda z: z.length)
    pts = [g.interpolate(i/n, normalized=True) for i in range(n+1)]
    edges = [obj(f"{prefix}{i+1:02d}", "network_edge", LineString([pts[i], pts[i+1]]), src, "subedge sampled from real OSM road") for i in range(n)]
    return edges, [f"N{i+1:02d}" for i in range(n+1)]


def edge_graph(objects: list[dict], snap: float = 0.01) -> dict[tuple, set]:
    graph = defaultdict(set)
    for o in objects:
        g = geom(o); a, b = Point(g.coords[0]), Point(g.coords[-1])
        ka, kb = (round(a.x/snap), round(a.y/snap)), (round(b.x/snap), round(b.y/snap))
        graph[ka].add(kb); graph[kb].add(ka)
    return graph


def reachable(graph, start, target) -> tuple[bool, int | None]:
    q = [(start, 0)]; seen = {start}
    while q:
        node, d = q.pop(0)
        if node == target: return True, d
        for nxt in graph.get(node, ()):
            if nxt not in seen: seen.add(nxt); q.append((nxt, d+1))
    return False, None


def add_o08(items: list[dict], base: list[dict], roads: gpd.GeoDataFrame) -> None:
    # Connected chain with a branch/dead end.
    edges, _ = split_chain(base[0], 5, "E")
    mid = geom(edges[2]).coords[-1]; branch = LineString([mid, (mid[0]+60, mid[1]+35)])
    edges.append(obj("E06", "network_edge", branch, base[0], "transparent branch from real road node"))
    build_graph_item(items, 1, edges, Point(geom(edges[0]).coords[0]), Point(geom(edges[4]).coords[-1]), "real-road chain with a branch")
    # Hard negative: two real roads cross in their interiors, but no endpoint node is supplied at the crossing.
    cross_pair = None
    sample = roads[roads.geometry.geom_type == "LineString"].head(5000)
    for i, a in sample.iterrows():
        hits = roads.sindex.query(a.geometry, predicate="crosses")
        if len(hits):
            cross_pair = (a, roads.loc[hits[0]]); break
    if cross_pair is None: raise RuntimeError("Could not find crossing roads for O08 hard negative")
    neg_edges = []
    for k, rec in enumerate(cross_pair):
        src = {"source_id": f"gis_osm_roads_free:{rec.osm_id}", "source_layer": "gis_osm_roads_free", "attributes": rec.to_dict()}
        e, _ = split_chain({**src, "geometry_wkt": shapely.to_wkt(rec.geometry)}, 3, f"E{k+1}")
        neg_edges.extend(e)
    build_graph_item(items, 2, neg_edges, Point(geom(neg_edges[0]).coords[0]), Point(geom(neg_edges[-1]).coords[-1]), "two interior-crossing roads without a shared endpoint node")
    # Connected path plus a disconnected distractor component.
    pos, _ = split_chain(base[2], 4, "E")
    dis, _ = split_chain(base[1], 2, "D")
    mix = pos + dis
    build_graph_item(items, 3, mix, Point(geom(pos[0]).coords[0]), Point(geom(pos[-1]).coords[-1]), "connected component plus disconnected distractor")


def build_graph_item(items: list[dict], idx: int, edges: list[dict], start: Point, target: Point, description: str) -> None:
    graph = edge_graph(edges); snap = .01
    ks, kt = (round(start.x/snap), round(start.y/snap)), (round(target.x/snap), round(target.y/snap))
    ok, steps = reachable(graph, ks, kt)
    os = edges + [obj("S", "start_node", start, derivation="edge endpoint"), obj("T", "target_node", target, derivation="edge endpoint")]
    items.append(make_item("O08", idx, os,
        "Using only shared edge endpoints as network nodes (interior geometric crossings do not create nodes), is target T reachable from start S?",
        "yes or no", "yes" if ok else "no", {"reachable": ok, "shortest_path_edges": steps, "node_snap_tolerance_m": snap, "edge_count": len(edges)}, {"type": "exact"}, construction=description, sufficiency={"wkt_exact": True, "geojson_centroid_bbox": False, "geojson_exact": True, "geojson_exact_attributes": True}))


def feature_collection(objects: list[dict], mode: str) -> dict:
    features = []
    for o in objects:
        g = geom(o); props = {"id": o["id"]}
        if mode == "geojson_centroid_bbox":
            props["bbox"] = [round(x, 6) for x in g.bounds]; out_geom = g.centroid
        else:
            out_geom = g
        if mode == "geojson_exact_attributes":
            props.update(o.get("source_attributes", {}))
        features.append({"type": "Feature", "properties": props, "geometry": mapping(out_geom)})
    return {"type": "FeatureCollection", "features": features}


def prompt_row(item: dict, mode: str) -> dict:
    if mode == "wkt_exact":
        payload = "\n".join(f"{o['id']}: {o['geometry_wkt']}" for o in item["objects"])
        intro = "Input (exact WKT):"
    else:
        payload = json.dumps(feature_collection(item["objects"], mode), ensure_ascii=False, separators=(",", ":"))
        intro = "Input (GeoJSON centroids with exact bboxes; original shapes are withheld):" if mode == "geojson_centroid_bbox" else "Input (exact GeoJSON with selected source attributes):" if mode.endswith("attributes") else "Input (exact GeoJSON geometry only):"
    content = f"CRS: EPSG:3006; coordinate units are metres.\nTask: {item['task']}\n{intro}\n{payload}\nAnswer format: {item['answer_format']}\nReturn only the answer."
    return {"prompt_id": f"{item['item_id']}--{mode}", "item_id": item["item_id"], "representation_mode": mode, "messages": [{"role": "user", "content": content}]}


def registry_outputs(source: Path, out: Path) -> None:
    rows = read_jsonl(source / "registry" / "l1_core_relation_registry_32.jsonl")
    by_id = {x["relation_id"]: copy.deepcopy(x) for x in rows}
    overrides = {
        "D01": {"canonical_operation": "centroid bearing classified into N/E/S/W; no diagonal labels"},
        "D04": {"scoring_rule": "circular angular error <= 1 degree"},
        "D05": {"observer_rule": "observer is fixed at the road's first stored coordinate and faces the last coordinate"},
        "M02": {"allowed_geometry_signatures": ["Polygon-Polygon", "MultiPolygon-MultiPolygon", "Polygon-MultiPolygon"]},
        "M08": {"canonical_operation": "symmetric densified discrete Hausdorff distance; Shapely densify=0.25; EPSG:3006 metres"},
        "M12": {"canonical_operation": "total polygon boundary length including all exterior and interior-hole rings"},
        "M14": {"canonical_operation": "closed buffer predicate intersects(B, buffer(A,r)), equivalent to distance(A,B)<=r"},
        "M15": {"candidate_count": 4, "candidate_ids_randomized": True},
        "M18": {"canonical_operation": "count only explicitly supplied POIs covered by the supplied region, including its boundary; divide by region area_km2"},
        "O01": {"route_direction": "stored coordinate order only"},
        "O02": {"question_scope": "pairwise before/after only"},
        "O03": {"reference_constraint": "straight reference line, or an explicitly stated local tangent; formal v1 uses straight lines only"},
        "O04": {"canonical_operation": "ordinary strict between for three collinear points"},
        "O06": {"canonical_operation": "compare numeric OSM layer only for intersecting/local road pairs; equal layer is a tie; layer is ordinal, not metres"},
        "O08": {"canonical_operation": "endpoint-node graph reachability; interior crossings without a shared node do not connect"},
    }
    active = []
    for rid in ACTIVE_IDS:
        row = by_id[rid]; row.update(overrides.get(rid, {})); row["v5_scope"] = "formal_active_28"; active.append(row)
    deferred = []
    for rid, reason in DEFERRED.items():
        row = by_id[rid]; row["v5_scope"] = "deferred_not_generated"; row["defer_reason"] = reason; deferred.append(row)
    write_jsonl(out / "registry" / "l1_active_relation_registry_28.jsonl", active)
    write_jsonl(out / "registry" / "l1_deferred_relations_4.jsonl", deferred)
    write_json(out / "registry" / "v5_formal_scope.json", {
        "inheritance_policy": "The immutable 32-relation v3.9 registry remains the provenance source; formal questions use an active 28-relation view.",
        "source_relation_count": 32, "active_relation_count": 28, "deferred_relation_count": 4,
        "active_ids": ACTIVE_IDS, "deferred": DEFERRED,
        "formal_non_topology_items": {"relations": 19, "examples_per_relation": 3, "canonical_items": 57, "representation_modes": 4, "prompts": 228},
    })


def load_osm(gpkg: Path) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    bbox = (17.95, 59.29, 18.12, 59.37)
    pois = gpd.read_file(gpkg, layer="gis_osm_pois_free", bbox=bbox).to_crs(3006)
    roads = gpd.read_file(gpkg, layer="gis_osm_roads_free", bbox=bbox).to_crs(3006)
    return pois, roads


def build(source: Path, gpkg: Path, out: Path) -> None:
    cases = read_jsonl(source / "data" / "l1_real_cases_32x3.jsonl")
    cases_by_id = defaultdict(list)
    for row in cases: cases_by_id[row["relation_id"]].append(row)
    points, lines, polys = source_pools(cases)
    pois, roads = load_osm(gpkg)
    items: list[dict] = []
    add_direction(items, points, lines)
    add_metrics(items, cases_by_id, points, lines, polys, pois)
    add_order(items, lines, roads)
    items.sort(key=lambda x: (NON_TOPO_IDS.index(x["relation_id"]), x["item_id"]))
    registry_outputs(source, out)
    write_jsonl(out / "data" / "non_topology_v1_items_57.jsonl", items)
    all_prompts = []
    for mode in MODES:
        rows = [prompt_row(x, mode) for x in items]
        write_jsonl(out / "prompts" / f"non_topology_v1_{mode}_57.jsonl", rows)
        all_prompts.extend(rows)
    write_jsonl(out / "prompts" / "non_topology_v1_all_modes_228.jsonl", all_prompts)
    gold = [{"item_id": x["item_id"], "relation_id": x["relation_id"], **x["gold"]} for x in items]
    write_jsonl(out / "preds" / "non_topology_v1_private_gold_57.jsonl", gold)
    counts = Counter(x["relation_id"] for x in items)
    write_json(out / "reports" / "non_topology_v1_build_report.json", {
        "canonical_items": len(items), "prompts": len(all_prompts), "relation_counts": dict(counts),
        "modes": MODES, "source_registry_preserved": True,
    })
    print(json.dumps({"items": len(items), "prompts": len(all_prompts), "relations": len(counts)}, ensure_ascii=False))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    ap.add_argument("--gpkg", type=Path, default=DEFAULT_GPKG)
    ap.add_argument("--out", type=Path, default=ROOT)
    args = ap.parse_args()
    build(args.source, args.gpkg, args.out)


if __name__ == "__main__":
    main()
