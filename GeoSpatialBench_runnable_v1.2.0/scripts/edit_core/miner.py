#!/usr/bin/env python3
"""Mine v5 formal-28 edit-only tasks from Paris 2km OSM (never generate F_new).

Each task must:
  - start from a scene where the target relation does NOT hold
  - be solvable only by an edit that actually realises that relation
  - reject copy-F1 / snap-to-equal / round-offset / already-true shortcuts
"""

from __future__ import annotations

import argparse
import json
import math
import random
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import geopandas as gpd
import pandas as pd
from shapely import affinity
from shapely.geometry import LineString, Point
from shapely.ops import nearest_points

from .factory import (  # noqa: E402
    CARDINAL_4,
    CARDINAL_8,
    CORE_28_IDS,
    EDIT_OPS,
    _ANGLES,
)
from .pools import cap_pools, feature_record, load_pools, localize  # noqa: E402
from .validate import (  # noqa: E402
    _bearing_deg,
    _cardinal,
    _line_side,
    _scene_map,
    check_constraint,
    evaluate_edit,
)

_CATEGORY = {
    **{f"T{k:02d}": "topology" for k in (1, 2, 3, 4, 5, 6, 8, 9, 11)},
    **{f"D{k:02d}": "directional" for k in range(1, 8)},
    **{f"M{k:02d}": "metric" for k in (1, 2, 4, 8, 12, 14, 15, 18)},
    **{f"O{k:02d}": "order" for k in (1, 2, 3, 4, 6, 7, 8, 9)},
}

# Copying F1 geometry is the intended solution only for topological equals.
_ALLOW_COPY_F1 = {"T01"}


def _unit(dx: float, dy: float) -> Tuple[float, float]:
    n = math.hypot(dx, dy) or 1.0
    return dx / n, dy / n


def _pt_dir(ref: Point, direction: str, dist: float) -> Point:
    rad = math.radians(_ANGLES[direction])
    return Point(ref.x + math.sin(rad) * dist, ref.y + math.cos(rad) * dist)


def _opposite(direction: str, labels: List[str]) -> str:
    idx = labels.index(direction)
    return labels[(idx + len(labels) // 2) % len(labels)]


def _heading_deg(line: LineString) -> float:
    c0, c1 = line.coords[0], line.coords[-1]
    return math.degrees(math.atan2(float(c1[0]) - float(c0[0]), float(c1[1]) - float(c0[1]))) % 360.0


def _ang_diff(a: float, b: float) -> float:
    return abs((a - b + 180.0) % 360.0 - 180.0)


def _observer_side(axis: LineString, pt: Any) -> str:
    coords = list(axis.coords)
    line_b = _bearing_deg(Point(coords[0]), Point(coords[-1]))
    origin = axis.interpolate(0.5, normalized=True)
    rel_ang = (_bearing_deg(origin, Point(pt.centroid.x, pt.centroid.y)) - line_b + 180) % 360 - 180
    if abs(rel_ang) <= 45:
        return "front"
    if abs(rel_ang) >= 135:
        return "back"
    return "left" if rel_ang < 0 else "right"


def _line_normal(line: LineString) -> Tuple[float, float]:
    coords = list(line.coords)
    x0, y0 = float(coords[0][0]), float(coords[0][1])
    x1, y1 = float(coords[-1][0]), float(coords[-1][1])
    nx, ny = -(y1 - y0), (x1 - x0)
    return _unit(nx, ny)


def _centroid_delta(subj: Any, edited: Any) -> Optional[Tuple[float, float]]:
    cs, ce = subj.centroid, edited.centroid
    if cs.is_empty or ce.is_empty:
        return None
    return ce.x - cs.x, ce.y - cs.y


def _kind(geom: Any) -> str:
    t = getattr(geom, "geom_type", "")
    if t == "Point":
        return "point"
    if "Line" in t:
        return "line"
    return "area"


def _xy(geom: Any) -> Tuple[float, float]:
    c = geom.centroid
    return float(c.x), float(c.y)


def _move_centroid(geom: Any, x: float, y: float) -> Any:
    cx, cy = _xy(geom)
    return affinity.translate(geom, xoff=x - cx, yoff=y - cy)


def _noun(kind: str, row: Optional[Any] = None) -> str:
    if kind == "point":
        return "POI"
    if kind == "line":
        return "road"
    if row is not None:
        cat = str((row.get("semantics") or {}).get("category") or "")
        if cat == "building":
            return "building"
        return "zone"
    return "polygon"


def _translate_to_touch(subj: Any, ref: Any) -> Optional[Any]:
    p0, p1 = nearest_points(subj, ref)
    dx, dy = p1.x - p0.x, p1.y - p0.y
    dist = math.hypot(dx, dy)
    if dist < 1e-4:
        return None
    lo, hi = 0.92, 1.08
    best = None
    for _ in range(28):
        mid = (lo + hi) / 2.0
        cand = affinity.translate(subj, xoff=dx * mid, yoff=dy * mid)
        if cand.touches(ref) and not cand.overlaps(ref):
            return cand
        if cand.intersects(ref) and not cand.touches(ref):
            hi = mid
            best = cand
        else:
            lo = mid
    if best is not None and best.touches(ref) and not best.overlaps(ref):
        return best
    return None


# Relations whose F0 type is fixed by the validator / task design.
_F0_LOCKED = {
    "T05": "line", "T06": "area", "T09": "area",
    "D06": "line", "M12": "area", "M15": "point", "M18": "point", "O08": "line",
}


class CityEditMiner:
    def __init__(
        self,
        pools,
        *,
        target: int = 10,
        seed: int = 42,
        city: str = "Paris",
        f0_cap: Optional[int] = None,
        task_id_prefix: str = "",
    ) -> None:
        self.bdf, self.gdf, self.rdf, self.pdf = pools
        self.target = target
        self.city = city
        self.source = city.lower()
        self.f0_cap = f0_cap
        self.task_id_prefix = task_id_prefix
        self.rng = random.Random(seed)
        self.tasks: List[Dict[str, Any]] = []
        self.counts: Counter[str] = Counter()
        self.f0_counts: Counter[str] = Counter()
        self.seen: set = set()
        frames = [df for df in (self.bdf, self.gdf) if df is not None and len(df)]
        self._adf = gpd.GeoDataFrame(
            pd.concat(frames, ignore_index=True) if frames else self.bdf,
            geometry="geometry",
        )

    def _df(self, kind: str):
        if kind == "point":
            return self.pdf
        if kind == "line":
            return self.rdf
        return self._adf

    def _pair_n(self, rid: str, pair: str) -> int:
        return sum(1 for t in self.tasks if t["relation_id"] == rid and t.get("geom_pair") == pair)

    def _pair_extra(self, pair: str) -> Dict[str, Any]:
        return {"geom_pair": pair}

    def _ok_geom(self, kind: str, geom: Any) -> bool:
        if kind == "point":
            return geom.geom_type == "Point"
        if kind == "line":
            return geom.geom_type == "LineString" and geom.length >= 10
        return geom.geom_type in ("Polygon", "MultiPolygon") and geom.area >= 15

    def full(self, rid: str) -> bool:
        return self.counts[rid] >= self.target

    def _shuffled(self, df) -> Iterable[Tuple[int, Any]]:
        idx = list(range(len(df)))
        self.rng.shuffle(idx)
        for i in idx:
            yield i, df.iloc[i]

    def add(
        self,
        rid: str,
        *,
        operation: str,
        instruction: str,
        constraints: List[Dict[str, Any]],
        features: List[Dict[str, Any]],
        gt: Any,
        rel_after: Any,
        extra: Optional[Dict[str, Any]] = None,
    ) -> bool:
        if self.full(rid):
            return False
        key = (rid, tuple(f.get("source_object_id") for f in features))
        if key in self.seen:
            return False
        n = self.counts[rid]
        tid = f"{self.task_id_prefix}{rid}-{n + 1:03d}" if self.task_id_prefix else f"{rid}_{n:03d}"
        rec: Dict[str, Any] = {
            "task_id": tid,
            "relation_id": rid,
            "category": _CATEGORY[rid],
            "source": self.source,
            "task_type": "constraint_driven_spatial_edit",
            "operation": operation,
            "allowed_operations": list(EDIT_OPS),
            "instruction": instruction.replace("Paris", self.city),
            "constraints": constraints,
            "scene_features": features,
            "target_feature_id": "F0",
            "ground_truth_wkt": gt.wkt,
            "relation_after_gt": rel_after,
        }
        if extra:
            rec.update(extra)

        scene = _scene_map(features)
        f0 = scene.get("F0")
        if f0 is None:
            return False
        f0_kind = _kind(f0)
        rec["f0_kind"] = f0_kind
        if not rec.get("geom_pair"):
            rec["geom_pair"] = f0_kind
        locked = _F0_LOCKED.get(rid)
        if self.f0_cap is not None and locked is None and self.f0_counts[f0_kind] >= self.f0_cap:
            return False
        ctx = rec.get("context") or {}
        ctx_init = {k: v for k, v in ctx.items() if k != "predicted_F0_layer"}
        for c in constraints:
            ok0, _ = check_constraint(c, f0, scene, context=ctx_init)
            if ok0:
                return False
            if rid != "O06":
                tiny = affinity.translate(f0, xoff=0.5, yoff=0.5)
                ok_tiny, _ = check_constraint(c, tiny, scene, context=ctx)
                if ok_tiny:
                    return False
            if rid not in _ALLOW_COPY_F1 and rid != "O06" and "F1" in scene:
                ok_copy, _ = check_constraint(c, scene["F1"], scene, context=ctx)
                if ok_copy:
                    return False

        ev = evaluate_edit(rec, gt.wkt, extra_context=rec.get("context"))
        if not ev.get("constraints_satisfied"):
            return False
        rec["_gt_check"] = {"score": ev["score"]}
        self.seen.add(key)
        self.tasks.append(rec)
        self.counts[rid] += 1
        self.f0_counts[f0_kind] += 1
        return True

    def pack(self, geoms: List[Any], rows: List[Any], fids: List[str]) -> List[Dict[str, Any]]:
        local, _, _ = localize(geoms)
        return [
            feature_record(fids[i], local[i], rows[i]["semantics"], rows[i]["fid"])
            for i in range(len(rows))
        ]

    def _c(self, rid: str, **kw: Any) -> List[Dict[str, Any]]:
        c: Dict[str, Any] = {"relation_id": rid, "object": "F0", **kw}
        return [c]

    def _iter_near(self, row0, kind1: str, min_d: float, max_d: float, limit: int = 30):
        g0 = row0.geometry
        df1 = self._df(kind1)
        sindex = df1.sindex
        hits = list(sindex.intersection(g0.buffer(max_d).bounds))[:limit]
        self.rng.shuffle(hits)
        for j in hits:
            r1 = df1.iloc[j]
            if r1["fid"] == row0["fid"]:
                continue
            g1 = r1.geometry
            if not self._ok_geom(kind1, g1):
                continue
            d = float(g0.distance(g1))
            if min_d <= d <= max_d:
                yield r1

    def _scale_wants(self, parts: List[int]) -> List[int]:
        total = sum(parts) or 1
        raw = [p * self.target / total for p in parts]
        out = [int(math.floor(x)) for x in raw]
        rem = self.target - sum(out)
        order = sorted(range(len(parts)), key=lambda i: raw[i] - out[i], reverse=True)
        for k in range(max(0, rem)):
            out[order[k % len(out)]] += 1
        return out

    def _fill_slots(self, rid: str, specs: List[Tuple[str, int, Any]]) -> None:
        wants = self._scale_wants([s[1] for s in specs])
        scaled = [(pair, w, fn) for (pair, _, fn), w in zip(specs, wants)]
        rank = {"area": 0, "zone": 0, "building": 0, "line": 1, "point": 2}
        scaled.sort(key=lambda s: rank.get(str(s[0]).split("-")[0], 3))
        for _pair, want, fn in scaled:
            fn(want)
        for _pair, want, fn in scaled:
            if self.full(rid):
                return
            fn(self.target)

    # ------------------------------------------------------------------ topology
    def gen_T01(self) -> None:
        def mine(kind: str, want: int) -> None:
            pair = f"{kind}-{kind}"
            df = self._df(kind)
            n = len(df)
            if n < 2:
                return
            for idx, srow in self._shuffled(df):
                if self.full("T01") or self._pair_n("T01", pair) >= want:
                    return
                g1 = srow.geometry
                if not self._ok_geom(kind, g1):
                    continue
                orow = df.iloc[(idx + 29) % n]
                ox, oy = _xy(orow.geometry)[0] - _xy(g1)[0], _xy(orow.geometry)[1] - _xy(g1)[1]
                if math.hypot(ox, oy) < 18:
                    continue
                f0 = affinity.translate(g1, xoff=ox, yoff=oy)
                if f0.equals(g1):
                    continue
                local, _, _ = localize([f0, g1])
                n0, n1 = _noun(kind, srow), _noun(kind, srow)
                self.add(
                    "T01", operation="snap",
                    instruction=f"Snap {n0} F0 onto F1 so they are topologically equal ({kind}-{kind}, Paris {srow['fid']}).",
                    constraints=self._c("T01", type="topology", relation="equals", reference="F1"),
                    features=self.pack([f0, g1], [srow, srow], ["F0", "F1"]),
                    gt=local[1], rel_after="equals", extra=self._pair_extra(pair),
                )

        self._fill_slots("T01", [
            ("point-point", 3, lambda w: mine("point", w)),
            ("line-line", 3, lambda w: mine("line", w)),
            ("area-area", 4, lambda w: mine("area", w)),
        ])

    def gen_T02(self) -> None:
        def mine_inside_out(k0: str, k1: str, want: int) -> None:
            pair = f"{k0}-{k1}"
            for _, srow in self._shuffled(self._df(k0)):
                if self.full("T02") or self._pair_n("T02", pair) >= want:
                    return
                subj = srow.geometry
                if not self._ok_geom(k0, subj):
                    continue
                for rrow in self._iter_near(srow, k1, 8, 70):
                    ref = rrow.geometry
                    if not subj.disjoint(ref):
                        continue
                    rp = ref.representative_point() if k1 == "area" else ref.centroid
                    init = _move_centroid(subj, rp.x, rp.y)
                    if init.disjoint(ref):
                        p0, p1 = nearest_points(subj, ref)
                        nrm = math.hypot(p1.x - p0.x, p1.y - p0.y) or 1.0
                        d0 = float(subj.distance(ref))
                        init = affinity.translate(subj, xoff=(p1.x - p0.x) / nrm * (d0 + 4), yoff=(p1.y - p0.y) / nrm * (d0 + 4))
                    if init.disjoint(ref) or init.equals(ref):
                        continue
                    dx, dy = init.centroid.x - subj.centroid.x, init.centroid.y - subj.centroid.y
                    local, _, _ = localize([init, ref])
                    gt = affinity.translate(local[0], xoff=-dx, yoff=-dy)
                    if self.add(
                        "T02", operation="translate",
                        instruction=f"F0 ({_noun(k0, srow)}) currently overlaps F1 ({_noun(k1, rrow)}). Move F0 until disjoint by at least 8 m (Paris {srow['fid']},{rrow['fid']}).",
                        constraints=self._c("T02", type="topology", relation="disjoint", reference="F1", min_distance=8),
                        features=self.pack([init, ref], [srow, rrow], ["F0", "F1"]),
                        gt=gt, rel_after="disjoint", extra=self._pair_extra(pair),
                    ):
                        break

        self._fill_slots("T02", [
            ("point-area", 3, lambda w: mine_inside_out("point", "area", w)),
            ("line-area", 3, lambda w: mine_inside_out("line", "area", w)),
            ("area-area", 4, lambda w: mine_inside_out("area", "area", w)),
        ])

    def gen_T03(self) -> None:
        def mine(k0: str, k1: str, want: int) -> None:
            pair = f"{k0}-{k1}"
            for _, srow in self._shuffled(self._df(k0)):
                if self.full("T03") or self._pair_n("T03", pair) >= want:
                    return
                subj = srow.geometry
                if not self._ok_geom(k0, subj):
                    continue
                for rrow in self._iter_near(srow, k1, 10, 80):
                    ref = rrow.geometry
                    if not subj.disjoint(ref):
                        continue
                    if k0 == "point" and k1 == "area":
                        rp = ref.representative_point()
                        edited = Point(rp.x, rp.y)
                    else:
                        p0, p1 = nearest_points(subj, ref)
                        dx, dy = p1.x - p0.x, p1.y - p0.y
                        n = math.hypot(dx, dy) or 1.0
                        d = float(subj.distance(ref))
                        edited = affinity.translate(subj, xoff=dx / n * (d + 2.8), yoff=dy / n * (d + 2.8))
                    if not edited.intersects(ref) or edited.equals(ref) or edited.touches(ref):
                        continue
                    local, ox, oy = localize([subj, ref])
                    if k0 == "point":
                        gt = Point(edited.x - ox, edited.y - oy)
                    else:
                        gt = affinity.translate(
                            local[0],
                            xoff=edited.centroid.x - subj.centroid.x,
                            yoff=edited.centroid.y - subj.centroid.y,
                        )
                    if self.add(
                        "T03", operation="translate",
                        instruction=f"Move {_noun(k0, srow)} F0 so it intersects {_noun(k1, rrow)} F1 (not equal/touching) (Paris {srow['fid']},{rrow['fid']}).",
                        constraints=self._c("T03", type="topology", relation="intersects", reference="F1", must_not=["equals", "touches"]),
                        features=self.pack([subj, ref], [srow, rrow], ["F0", "F1"]),
                        gt=gt, rel_after="intersects", extra=self._pair_extra(pair),
                    ):
                        break

        self._fill_slots("T03", [
            ("point-area", 3, lambda w: mine("point", "area", w)),
            ("line-line", 3, lambda w: mine("line", "line", w)),
            ("line-area", 2, lambda w: mine("line", "area", w)),
            ("area-area", 2, lambda w: mine("area", "area", w)),
        ])

    def gen_T04(self) -> None:
        def mine_poly(want: int) -> None:
            pair = "area-area"
            sindex = self._adf.sindex
            for _, srow in self._shuffled(self._adf):
                if self.full("T04") or self._pair_n("T04", pair) >= want:
                    return
                subj = srow.geometry
                if not self._ok_geom("area", subj):
                    continue
                for j in list(sindex.intersection(subj.buffer(80).bounds))[:25]:
                    rrow = self._adf.iloc[j]
                    if rrow["fid"] == srow["fid"]:
                        continue
                    ref = rrow.geometry
                    d0 = float(subj.distance(ref))
                    if d0 < 4 or d0 > 70:
                        continue
                    edited = _translate_to_touch(subj, ref)
                    if edited is None:
                        continue
                    dx, dy = edited.centroid.x - subj.centroid.x, edited.centroid.y - subj.centroid.y
                    local, _, _ = localize([subj, ref])
                    gt = affinity.translate(local[0], xoff=dx, yoff=dy)
                    if not gt.touches(local[1]) or gt.overlaps(local[1]):
                        continue
                    if self.add(
                        "T04", operation="translate",
                        instruction=f"Move polygon F0 to touch F1 (shared boundary, no overlap) (Paris {srow['fid']},{rrow['fid']}).",
                        constraints=self._c("T04", type="topology", relation="touches", reference="F1", must_not=["overlaps", "equals"]),
                        features=self.pack([subj, ref], [srow, rrow], ["F0", "F1"]),
                        gt=gt, rel_after="touches", extra=self._pair_extra(pair),
                    ):
                        break

        def mine_point_line(want: int) -> None:
            pair = "point-line"
            for _, prow in self._shuffled(self.pdf):
                if self.full("T04") or self._pair_n("T04", pair) >= want:
                    return
                poi = prow.geometry
                for rrow in self._iter_near(prow, "line", 6, 80):
                    line = rrow.geometry
                    if poi.touches(line):
                        continue
                    end = Point(line.coords[-1])
                    local, ox, oy = localize([poi, line])
                    gt = Point(end.x - ox, end.y - oy)
                    if self.add(
                        "T04", operation="translate",
                        instruction=f"Move POI F0 to touch road F1 at an endpoint (Paris {prow['fid']},{rrow['fid']}).",
                        constraints=self._c("T04", type="topology", relation="touches", reference="F1", must_not=["overlaps", "equals"]),
                        features=self.pack([poi, line], [prow, rrow], ["F0", "F1"]),
                        gt=gt, rel_after="touches", extra=self._pair_extra(pair),
                    ):
                        break

        def mine_line_line(want: int) -> None:
            pair = "line-line"
            n = len(self.rdf)
            for i, rrow in self._shuffled(self.rdf):
                if self.full("T04") or self._pair_n("T04", pair) >= want:
                    return
                f1 = rrow.geometry
                if not self._ok_geom("line", f1):
                    continue
                end = Point(f1.coords[-1])
                for k in range(1, 35):
                    orow = self.rdf.iloc[(i + 9 * k) % n]
                    if orow["fid"] == rrow["fid"]:
                        continue
                    f0 = orow.geometry
                    if not self._ok_geom("line", f0) or not f0.disjoint(f1) or float(f0.distance(f1)) < 12:
                        continue
                    start0 = Point(f0.coords[0])
                    dx, dy = end.x - start0.x, end.y - start0.y
                    connected = affinity.translate(f0, xoff=dx, yoff=dy)
                    if not connected.touches(f1) or connected.equals(f1):
                        continue
                    local, _, _ = localize([f0, f1])
                    gt = affinity.translate(local[0], xoff=dx, yoff=dy)
                    if self.add(
                        "T04", operation="translate",
                        instruction=f"Move road F0 to touch road F1 at an endpoint (Paris {orow['fid']},{rrow['fid']}).",
                        constraints=self._c("T04", type="topology", relation="touches", reference="F1", must_not=["overlaps", "equals"]),
                        features=self.pack([f0, f1], [orow, rrow], ["F0", "F1"]),
                        gt=gt, rel_after="touches", extra=self._pair_extra(pair),
                    ):
                        break

        self._fill_slots("T04", [
            ("area-area", 4, mine_poly),
            ("point-line", 3, mine_point_line),
            ("line-line", 3, mine_line_line),
        ])

    def gen_T05(self) -> None:
        def mine(k1: str, want: int) -> None:
            pair = f"line-{k1}"
            df1 = self._df(k1)
            sindex = df1.sindex
            for _, rrow in self._shuffled(self.rdf):
                if self.full("T05") or self._pair_n("T05", pair) >= want:
                    return
                line = rrow.geometry
                if not self._ok_geom("line", line):
                    continue
                for j in list(sindex.intersection(line.buffer(90).bounds))[:20]:
                    brow = df1.iloc[j]
                    if brow["fid"] == rrow["fid"]:
                        continue
                    other = brow.geometry
                    if not self._ok_geom(k1, other):
                        continue
                    if line.crosses(other) or line.intersects(other):
                        continue
                    d = float(line.distance(other))
                    if d < 6 or d > 80:
                        continue
                    mid = line.interpolate(0.5, normalized=True)
                    rp = other.representative_point() if k1 == "area" else other.interpolate(0.5, normalized=True)
                    moved = affinity.translate(line, xoff=rp.x - mid.x, yoff=rp.y - mid.y)
                    if not moved.crosses(other):
                        continue
                    local, _, _ = localize([line, other])
                    gt = affinity.translate(local[0], xoff=rp.x - mid.x, yoff=rp.y - mid.y)
                    if not gt.crosses(local[1]):
                        continue
                    if self.add(
                        "T05", operation="translate",
                        instruction=f"Move road F0 so it crosses {_noun(k1, brow)} F1 (Paris {rrow['fid']},{brow['fid']}).",
                        constraints=self._c("T05", type="topology", relation="crosses", reference="F1"),
                        features=self.pack([line, other], [rrow, brow], ["F0", "F1"]),
                        gt=gt, rel_after="crosses", extra=self._pair_extra(pair),
                    ):
                        break

        self._fill_slots("T05", [
            ("line-line", 5, lambda w: mine("line", w)),
            ("line-area", 5, lambda w: mine("area", w)),
        ])

    def gen_T06(self) -> None:
        def mine(k1: str, want: int) -> None:
            pair = f"area-{k1}"
            for _, grow in self._shuffled(self._adf):
                if self.full("T06") or self._pair_n("T06", pair) >= want:
                    return
                big = grow.geometry
                if big.area < 120:
                    continue
                for rrow in self._iter_near(grow, k1, 8, 180, limit=25):
                    small = rrow.geometry
                    if k1 == "area" and small.area >= big.area * 0.35:
                        continue
                    if k1 == "line" and small.length > 60:
                        continue
                    if big.contains(small) or big.covers(small):
                        continue
                    cx, cy = _xy(small)[0] - _xy(big)[0], _xy(small)[1] - _xy(big)[1]
                    moved = affinity.translate(big, xoff=cx, yoff=cy)
                    if not moved.contains(small) or moved.equals(small):
                        continue
                    local, _, _ = localize([big, small])
                    gt = affinity.translate(local[0], xoff=cx, yoff=cy)
                    if self.add(
                        "T06", operation="translate",
                        instruction=f"Move zone F0 so it contains {_noun(k1, rrow)} F1 in its interior (Paris {grow['fid']},{rrow['fid']}).",
                        constraints=self._c("T06", type="topology", relation="contains", reference="F1", must_not=["equals"]),
                        features=self.pack([big, small], [grow, rrow], ["F0", "F1"]),
                        gt=gt, rel_after="contains", extra=self._pair_extra(pair),
                    ):
                        break

        self._fill_slots("T06", [
            ("area-point", 3, lambda w: mine("point", w)),
            ("area-line", 3, lambda w: mine("line", w)),
            ("area-area", 4, lambda w: mine("area", w)),
        ])

    def gen_T08(self) -> None:
        def mine_area(want: int) -> None:
            pair = "area-area"
            sindex = self._adf.sindex
            for _, srow in self._shuffled(self._adf):
                if self.full("T08") or self._pair_n("T08", pair) >= want:
                    return
                subj = srow.geometry
                if not self._ok_geom("area", subj):
                    continue
                for j in list(sindex.intersection(subj.buffer(60).bounds))[:30]:
                    rrow = self._adf.iloc[j]
                    if rrow["fid"] == srow["fid"] or not subj.disjoint(rrow.geometry):
                        continue
                    ref = rrow.geometry
                    d = float(subj.distance(ref))
                    if d < 1.5 or d > 40:
                        continue
                    p0, p1 = nearest_points(subj, ref)
                    ux, uy = _unit(p1.x - p0.x, p1.y - p0.y)
                    placed = False
                    for frac in (0.35, 0.5, 0.65, 0.8, 1.1):
                        step = d * frac + 0.8
                        edited = affinity.translate(subj, xoff=ux * step, yoff=uy * step)
                        if not edited.overlaps(ref) or edited.contains(ref) or ref.contains(edited):
                            continue
                        local, _, _ = localize([subj, ref])
                        gt = affinity.translate(local[0], xoff=ux * step, yoff=uy * step)
                        if self.add(
                            "T08", operation="translate",
                            instruction=f"Move polygon F0 so it partially overlaps F1 (Paris {srow['fid']},{rrow['fid']}).",
                            constraints=self._c("T08", type="topology", relation="overlaps", reference="F1", must_not=["equals", "contains", "covers"]),
                            features=self.pack([subj, ref], [srow, rrow], ["F0", "F1"]),
                            gt=gt, rel_after="overlaps", extra=self._pair_extra(pair),
                        ):
                            placed = True
                            break
                    if placed:
                        break

        def mine_line(want: int) -> None:
            pair = "line-line"
            n = len(self.rdf)
            for i, rrow in self._shuffled(self.rdf):
                if self.full("T08") or self._pair_n("T08", pair) >= want:
                    return
                f1 = rrow.geometry
                if not self._ok_geom("line", f1):
                    continue
                h1 = _heading_deg(f1)
                for k in range(1, 40):
                    orow = self.rdf.iloc[(i + 11 * k) % n]
                    if orow["fid"] == rrow["fid"]:
                        continue
                    f0 = orow.geometry
                    if not self._ok_geom("line", f0) or not f0.disjoint(f1):
                        continue
                    h0 = _heading_deg(f0)
                    if _ang_diff(h0, h1) > 25 and _ang_diff(h0, (h1 + 180) % 360) > 25:
                        continue
                    p = f1.interpolate(0.22, normalized=True)
                    start = Point(f0.coords[0])
                    moved = affinity.translate(f0, xoff=p.x - start.x, yoff=p.y - start.y)
                    dhead = (h1 - _heading_deg(moved) + 180) % 360 - 180
                    rot = affinity.rotate(moved, -dhead, origin=p)
                    if not rot.overlaps(f1) or rot.equals(f1):
                        continue
                    local, ox, oy = localize([f0, f1])
                    gt = affinity.rotate(
                        affinity.translate(local[0], xoff=p.x - start.x, yoff=p.y - start.y),
                        -dhead, origin=Point(p.x - ox, p.y - oy),
                    )
                    if self.add(
                        "T08", operation="translate",
                        instruction=f"Move road F0 so it overlaps road F1 along a shared stretch (Paris {orow['fid']},{rrow['fid']}).",
                        constraints=self._c("T08", type="topology", relation="overlaps", reference="F1", must_not=["equals", "contains", "covers"]),
                        features=self.pack([f0, f1], [orow, rrow], ["F0", "F1"]),
                        gt=gt, rel_after="overlaps", extra=self._pair_extra(pair),
                    ):
                        break

        self._fill_slots("T08", [
            ("area-area", 6, mine_area),
            ("line-line", 4, mine_line),
        ])

    def gen_T09(self) -> None:
        def mine_point(want: int) -> None:
            pair = "area-point"
            p_sindex = self.pdf.sindex
            for _, grow in self._shuffled(self._adf):
                if self.full("T09") or self._pair_n("T09", pair) >= want:
                    return
                big = grow.geometry
                if big.area < 80:
                    continue
                for j in list(p_sindex.intersection(big.buffer(160).bounds))[:20]:
                    prow = self.pdf.iloc[j]
                    poi = prow.geometry
                    if big.covers(poi) or big.contains(poi):
                        continue
                    d = float(big.distance(poi))
                    if d < 8 or d > 120:
                        continue
                    p0, p1 = nearest_points(big.boundary, poi)
                    dx, dy = p1.x - p0.x, p1.y - p0.y
                    moved = affinity.translate(big, xoff=dx, yoff=dy)
                    if not (moved.covers(poi) and not moved.contains(poi)):
                        continue
                    local, _, _ = localize([big, poi])
                    gt = affinity.translate(local[0], xoff=dx, yoff=dy)
                    if self.add(
                        "T09", operation="translate",
                        instruction=f"Move zone F0 so it covers POI F1 on its boundary (Paris {grow['fid']},{prow['fid']}).",
                        constraints=self._c("T09", type="topology", relation="covers", reference="F1", must_not=["equals", "contains"]),
                        features=self.pack([big, poi], [grow, prow], ["F0", "F1"]),
                        gt=gt, rel_after="covers", extra=self._pair_extra(pair),
                    ):
                        break

        def mine_line(want: int) -> None:
            pair = "area-line"
            r_sindex = self.rdf.sindex
            for _, grow in self._shuffled(self._adf):
                if self.full("T09") or self._pair_n("T09", pair) >= want:
                    return
                big = grow.geometry
                if big.area < 120:
                    continue
                for j in list(r_sindex.intersection(big.buffer(120).bounds))[:18]:
                    rrow = self.rdf.iloc[j]
                    line = rrow.geometry
                    if line.length > 40 or big.covers(line) or big.contains(line):
                        continue
                    d = float(big.distance(line))
                    if d < 8 or d > 100:
                        continue
                    p0, p1 = nearest_points(big.boundary, line)
                    dx, dy = p1.x - p0.x, p1.y - p0.y
                    moved = affinity.translate(big, xoff=dx, yoff=dy)
                    if not (moved.covers(line) and not moved.contains(line)):
                        continue
                    local, _, _ = localize([big, line])
                    gt = affinity.translate(local[0], xoff=dx, yoff=dy)
                    if self.add(
                        "T09", operation="translate",
                        instruction=f"Move zone F0 so it covers road F1 along the boundary (Paris {grow['fid']},{rrow['fid']}).",
                        constraints=self._c("T09", type="topology", relation="covers", reference="F1", must_not=["equals", "contains"]),
                        features=self.pack([big, line], [grow, rrow], ["F0", "F1"]),
                        gt=gt, rel_after="covers", extra=self._pair_extra(pair),
                    ):
                        break

        def mine_area(want: int) -> None:
            pair = "area-area"
            for _, grow in self._shuffled(self._adf):
                if self.full("T09") or self._pair_n("T09", pair) >= want:
                    return
                big = grow.geometry
                if big.area < 150:
                    continue
                for rrow in self._iter_near(grow, "area", 8, 150, limit=20):
                    small = rrow.geometry
                    if small.area >= big.area * 0.4:
                        continue
                    cx, cy = _xy(small)[0] - _xy(big)[0], _xy(small)[1] - _xy(big)[1]
                    contained = affinity.translate(big, xoff=cx, yoff=cy)
                    if not contained.contains(small):
                        continue
                    ux, uy = _unit(contained.centroid.x - small.centroid.x, contained.centroid.y - small.centroid.y)
                    best = None
                    lo, hi = 0.0, 80.0
                    for _ in range(24):
                        mid = (lo + hi) / 2.0
                        cand = affinity.translate(contained, ux * mid, uy * mid)
                        if cand.covers(small) and not cand.contains(small):
                            best = cand
                            break
                        if cand.contains(small):
                            lo = mid
                        else:
                            hi = mid
                    if best is None:
                        continue
                    dx, dy = best.centroid.x - big.centroid.x, best.centroid.y - big.centroid.y
                    local, _, _ = localize([big, small])
                    gt = affinity.translate(local[0], xoff=dx, yoff=dy)
                    if self.add(
                        "T09", operation="translate",
                        instruction=f"Move zone F0 so it covers polygon F1 (covers, not contains) (Paris {grow['fid']},{rrow['fid']}).",
                        constraints=self._c("T09", type="topology", relation="covers", reference="F1", must_not=["equals", "contains"]),
                        features=self.pack([big, small], [grow, rrow], ["F0", "F1"]),
                        gt=gt, rel_after="covers", extra=self._pair_extra(pair),
                    ):
                        break

        self._fill_slots("T09", [
            ("area-point", 4, mine_point),
            ("area-line", 3, mine_line),
            ("area-area", 3, mine_area),
        ])

    def gen_T11(self) -> None:
        def mine(k0: str, want: int) -> None:
            pair = f"{k0}-area"
            for _, grow in self._shuffled(self._adf):
                if self.full("T11") or self._pair_n("T11", pair) >= want:
                    return
                outer = grow.geometry
                if outer.area < 150:
                    continue
                rp = outer.representative_point()
                try:
                    if not outer.contains_properly(rp):
                        continue
                except Exception:
                    if not outer.contains(rp):
                        continue
                for srow in self._iter_near(grow, k0, 10, 180, limit=25):
                    inner = srow.geometry
                    if k0 == "line" and inner.length > 35:
                        continue
                    if k0 == "area" and inner.area >= outer.area * 0.25:
                        continue
                    if outer.contains(inner) or outer.covers(inner):
                        continue
                    parked = inner
                    inside = _move_centroid(inner, rp.x, rp.y)
                    try:
                        ok = bool(outer.contains_properly(inside))
                    except Exception:
                        ok = bool(outer.contains(inside) and not outer.touches(inside))
                    if not ok:
                        continue
                    local, ox, oy = localize([parked, outer])
                    gt = _move_centroid(local[0], rp.x - ox, rp.y - oy)
                    if self.add(
                        "T11", operation="translate",
                        instruction=f"Move {_noun(k0, srow)} F0 strictly inside zone F1 (not on the boundary) (Paris {srow['fid']},{grow['fid']}).",
                        constraints=self._c("T11", type="topology", relation="contains_properly", reference="F1"),
                        features=self.pack([parked, outer], [srow, grow], ["F0", "F1"]),
                        gt=gt, rel_after="contains_properly", extra=self._pair_extra(pair),
                    ):
                        break

        self._fill_slots("T11", [
            ("point-area", 4, lambda w: mine("point", w)),
            ("line-area", 3, lambda w: mine("line", w)),
            ("area-area", 3, lambda w: mine("area", w)),
        ])

    # ------------------------------------------------------------------ directional
    def _gen_cardinal(self, rid: str, labels: List[str], n: int) -> None:
        slots = [("point", "point", 4), ("area", "area", 3), ("line", "area", 3)]

        def mine(k0: str, k1: str, want: int) -> None:
            pair = f"{k0}-{k1}"
            df0 = self._df(k0)
            for i, r0 in self._shuffled(df0):
                if self.full(rid) or self._pair_n(rid, pair) >= want:
                    return
                if not self._ok_geom(k0, r0.geometry):
                    continue
                direction = labels[i % len(labels)]
                for r1 in self._iter_near(r0, k1, 20, 180, limit=40):
                    f0, f1 = r0.geometry, r1.geometry
                    got = _cardinal(_bearing_deg(f1.centroid, f0.centroid), n)
                    if got == direction:
                        continue
                    dist = 14.0 + (i % 9) * 1.7
                    x1, y1 = _xy(f1)
                    gt_w = _pt_dir(Point(x1, y1), direction, dist)
                    local, ox, oy = localize([f0, f1])
                    gt = _move_centroid(local[0], gt_w.x - ox, gt_w.y - oy)
                    if self.add(
                        rid, operation="translate",
                        instruction=f"Move {_noun(k0, r0)} F0 to the {direction} of {_noun(k1, r1)} F1 ({n}-sector) (Paris {r1['fid']}).",
                        constraints=self._c(rid, type="directional", relation=f"cardinal_{n}", reference="F1", value=direction, min_sep=8),
                        features=self.pack([f0, f1], [r0, r1], ["F0", "F1"]),
                        gt=gt, rel_after={"direction": direction}, extra=self._pair_extra(pair),
                    ):
                        break

        self._fill_slots(rid, [(f"{a}-{b}", w, lambda w, a=a, b=b: mine(a, b, w)) for a, b, w in slots])

    def gen_D01(self) -> None:
        self._gen_cardinal("D01", CARDINAL_4, 4)

    def gen_D02(self) -> None:
        self._gen_cardinal("D02", CARDINAL_8, 8)

    def gen_D04(self) -> None:
        def mine(k0: str, k1: str, want: int) -> None:
            pair = f"{k0}-{k1}"
            for i, r0 in self._shuffled(self._df(k0)):
                if self.full("D04") or self._pair_n("D04", pair) >= want:
                    return
                if not self._ok_geom(k0, r0.geometry):
                    continue
                ang = 25 + (i % 10) * 31
                for r1 in self._iter_near(r0, k1, 20, 160, limit=35):
                    f0, f1 = r0.geometry, r1.geometry
                    cur = _bearing_deg(f1.centroid, f0.centroid)
                    if _ang_diff(cur, ang) < 35:
                        continue
                    dist = 15.0 + (i % 6) * 1.3
                    x1, y1 = _xy(f1)
                    rad = math.radians(ang)
                    gt_w = Point(x1 + dist * math.sin(rad), y1 + dist * math.cos(rad))
                    local, ox, oy = localize([f0, f1])
                    gt = _move_centroid(local[0], gt_w.x - ox, gt_w.y - oy)
                    if self.add(
                        "D04", operation="translate",
                        instruction=f"Move {_noun(k0, r0)} F0 to bearing {ang}° from {_noun(k1, r1)} F1 (Paris {r1['fid']}).",
                        constraints=self._c("D04", type="directional", relation="bearing", reference="F1", value_deg=ang, min_sep=8, tolerance_deg=12),
                        features=self.pack([f0, f1], [r0, r1], ["F0", "F1"]),
                        gt=gt, rel_after={"bearing_deg": ang}, extra=self._pair_extra(pair),
                    ):
                        break

        self._fill_slots("D04", [
            ("point-point", 4, lambda w: mine("point", "point", w)),
            ("area-area", 3, lambda w: mine("area", "area", w)),
            ("line-area", 3, lambda w: mine("line", "area", w)),
        ])

    def gen_D05(self) -> None:
        sides = ("left", "right", "front", "back")

        def mine(k0: str, want: int) -> None:
            pair = f"{k0}-line"
            for i, rrow in self._shuffled(self.rdf):
                if self.full("D05") or self._pair_n("D05", pair) >= want:
                    return
                axis = rrow.geometry
                if not self._ok_geom("line", axis) or axis.length < 20:
                    continue
                side = sides[i % 4]
                coords = list(axis.coords)
                start, end = Point(coords[0]), Point(coords[-1])
                mid = axis.interpolate(0.5, normalized=True)
                heading = math.atan2(end.x - start.x, end.y - start.y)
                if side == "front":
                    tx, ty = end.x + 14 * math.sin(heading), end.y + 14 * math.cos(heading)
                elif side == "back":
                    tx, ty = start.x - 14 * math.sin(heading), start.y - 14 * math.cos(heading)
                elif side == "left":
                    tx, ty = mid.x - 12 * math.cos(heading), mid.y + 12 * math.sin(heading)
                else:
                    tx, ty = mid.x + 12 * math.cos(heading), mid.y - 12 * math.sin(heading)
                for srow in self._iter_near(rrow, k0, 15, 160, limit=30):
                    f0 = srow.geometry
                    if _observer_side(axis, f0) == side:
                        continue
                    local, ox, oy = localize([f0, axis])
                    gt = _move_centroid(local[0], tx - ox, ty - oy)
                    if self.add(
                        "D05", operation="translate",
                        instruction=f"Move {_noun(k0, srow)} F0 to the {side} of directed road F1 (Paris {rrow['fid']}).",
                        constraints=self._c("D05", type="directional", relation="relative_observer", reference="F1", value=side, min_sep=8),
                        features=self.pack([f0, axis], [srow, rrow], ["F0", "F1"]),
                        gt=gt, rel_after={"relative_direction": side}, extra=self._pair_extra(pair),
                    ):
                        break

        self._fill_slots("D05", [
            ("point-line", 4, lambda w: mine("point", w)),
            ("area-line", 3, lambda w: mine("area", w)),
            ("line-line", 3, lambda w: mine("line", w)),
        ])

    def gen_D06(self) -> None:
        for i, rrow in self._shuffled(self.rdf):
            if self.full("D06"):
                return
            line = rrow.geometry
            if not self._ok_geom("line", line):
                continue
            orientation = CARDINAL_8[i % 8]
            ang = _ANGLES[orientation]
            if _ang_diff(_heading_deg(line), ang) < 40:
                continue
            local, _, _ = localize([line])
            ln = local[0]
            start = Point(ln.coords[0])
            length = float(ln.length)
            rad = math.radians(ang)
            gt = LineString([(start.x, start.y), (start.x + length * math.sin(rad), start.y + length * math.cos(rad))])
            self.add(
                "D06", operation="rotate",
                instruction=f"Rotate road F0 so heading becomes {orientation} (Paris {rrow['fid']}).",
                constraints=self._c("D06", type="directional", relation="object_orientation", value=orientation),
                features=[feature_record("F0", ln, rrow["semantics"], rrow["fid"])],
                gt=gt, rel_after={"orientation": orientation},                 extra=self._pair_extra("line"),
            )

    # ------------------------------------------------------------------ metric
    def _mine_distance_band(self, rid: str, metric: str, k0: str, k1: str, want: int, dmin: float, dmax: float, init_lo: float, init_hi: float) -> None:
        pair = f"{k0}-{k1}"
        for _, srow in self._shuffled(self._df(k0)):
            if self.full(rid) or self._pair_n(rid, pair) >= want:
                return
            subj = srow.geometry
            if not self._ok_geom(k0, subj):
                continue
            for rrow in self._iter_near(srow, k1, init_lo, init_hi, limit=25):
                ref = rrow.geometry
                if metric == "boundary_distance":
                    b0 = subj if subj.geom_type == "Point" else subj.boundary
                    b1 = ref if ref.geom_type == "Point" else ref.boundary
                    d0 = float(b0.distance(b1))
                elif metric == "manhattan_distance":
                    d0 = abs(_xy(subj)[0] - _xy(ref)[0]) + abs(_xy(subj)[1] - _xy(ref)[1])
                else:
                    d0 = float(subj.distance(ref))
                if d0 < init_lo or d0 > init_hi:
                    continue
                target = 0.5 * (dmin + dmax)
                p0, p1 = nearest_points(subj, ref)
                ux, uy = _unit(p1.x - p0.x, p1.y - p0.y)
                step = d0 - target
                edited = affinity.translate(subj, xoff=ux * step, yoff=uy * step)
                if metric == "boundary_distance":
                    be = edited if edited.geom_type == "Point" else edited.boundary
                    br = ref if ref.geom_type == "Point" else ref.boundary
                    de = float(be.distance(br))
                elif metric == "manhattan_distance":
                    x1, y1 = _xy(ref)
                    edited = _move_centroid(subj, x1 + 10.0, y1 + 8.0)
                    de = 18.0
                else:
                    de = float(edited.distance(ref))
                if de < dmin or de > dmax or not edited.disjoint(ref):
                    continue
                delta = _centroid_delta(subj, edited)
                if delta is None:
                    continue
                local, _, _ = localize([subj, ref])
                gt = affinity.translate(local[0], xoff=delta[0], yoff=delta[1])
                op_kw = dict(op="<=", value=dmax, min_value=dmin, require_disjoint=True, unit="m")
                if metric == "within_distance":
                    op_kw["threshold"] = dmax
                if self.add(
                    rid, operation="translate",
                    instruction=f"Move {_noun(k0, srow)} F0 so {metric.replace('_', ' ')} to {_noun(k1, rrow)} F1 is between {dmin:.0f} and {dmax:.0f} m, disjoint (Paris {srow['fid']},{rrow['fid']}).",
                    constraints=self._c(rid, type="metric", metric=metric, reference="F1", **op_kw),
                    features=self.pack([subj, ref], [srow, rrow], ["F0", "F1"]),
                    gt=gt, rel_after={"distance_m": de}, extra=self._pair_extra(pair),
                ):
                    break

    def gen_M01(self) -> None:
        self._fill_slots("M01", [
            ("point-point", 3, lambda w: self._mine_distance_band("M01", "euclidean_distance", "point", "point", w, 8, 22, 40, 110)),
            ("point-area", 3, lambda w: self._mine_distance_band("M01", "euclidean_distance", "point", "area", w, 8, 22, 40, 110)),
            ("area-area", 2, lambda w: self._mine_distance_band("M01", "euclidean_distance", "area", "area", w, 8, 22, 40, 110)),
            ("line-area", 2, lambda w: self._mine_distance_band("M01", "euclidean_distance", "line", "area", w, 8, 22, 40, 110)),
        ])

    def gen_M02(self) -> None:
        self._fill_slots("M02", [
            ("area-area", 4, lambda w: self._mine_distance_band("M02", "boundary_distance", "area", "area", w, 4, 12, 25, 80)),
            ("line-area", 3, lambda w: self._mine_distance_band("M02", "boundary_distance", "line", "area", w, 4, 12, 25, 80)),
            ("line-line", 3, lambda w: self._mine_distance_band("M02", "boundary_distance", "line", "line", w, 4, 12, 25, 80)),
        ])

    def gen_M04(self) -> None:
        self._fill_slots("M04", [
            ("point-point", 4, lambda w: self._mine_distance_band("M04", "manhattan_distance", "point", "point", w, 12, 36, 70, 220)),
            ("area-area", 3, lambda w: self._mine_distance_band("M04", "manhattan_distance", "area", "area", w, 12, 36, 70, 220)),
            ("point-area", 3, lambda w: self._mine_distance_band("M04", "manhattan_distance", "point", "area", w, 12, 36, 70, 220)),
        ])

    def gen_M08(self) -> None:
        def mine(k0: str, k1: str, want: int) -> None:
            pair = f"{k0}-{k1}"
            df1 = self._df(k1)
            n1 = len(df1)
            for i, r0 in self._shuffled(self._df(k0)):
                if self.full("M08") or self._pair_n("M08", pair) >= want:
                    return
                f0 = r0.geometry
                if not self._ok_geom(k0, f0):
                    continue
                for k in range(1, 40):
                    r1 = df1.iloc[(i + 13 * k) % n1]
                    if r1["fid"] == r0["fid"]:
                        continue
                    f1 = r1.geometry
                    if not self._ok_geom(k1, f1):
                        continue
                    cd = float(f0.centroid.distance(f1.centroid))
                    if cd < 60 or cd > 280:
                        continue
                    aligned = _move_centroid(f0, *_xy(f1))
                    hd_align = float(aligned.hausdorff_distance(f1))
                    hd0 = float(f0.hausdorff_distance(f1))
                    if hd_align < 10 or hd_align > 90 or hd0 < hd_align + 25:
                        continue
                    threshold = hd_align + 8.0
                    local, _, _ = localize([f0, f1])
                    gt = _move_centroid(local[0], *_xy(local[1]))
                    if float(gt.hausdorff_distance(local[1])) > threshold:
                        continue
                    if self.add(
                        "M08", operation="translate",
                        instruction=f"Move {_noun(k0, r0)} F0 so Hausdorff distance to {_noun(k1, r1)} F1 is between 4 and {threshold:.0f} m (Paris {r0['fid']},{r1['fid']}).",
                        constraints=self._c("M08", type="metric", metric="hausdorff_distance", reference="F1", op="<=", value=threshold, min_value=4, unit="m"),
                        features=self.pack([f0, f1], [r0, r1], ["F0", "F1"]),
                        gt=gt, rel_after={"hausdorff_distance_m": float(gt.hausdorff_distance(local[1]))}, extra=self._pair_extra(pair),
                    ):
                        break

        self._fill_slots("M08", [
            ("line-line", 4, lambda w: mine("line", "line", w)),
            ("area-area", 3, lambda w: mine("area", "area", w)),
            ("line-area", 3, lambda w: mine("line", "area", w)),
        ])

    def gen_M12(self) -> None:
        def mine(src, want: int, pair: str) -> None:
            for _, srow in self._shuffled(src):
                if self.full("M12") or self._pair_n("M12", pair) >= want:
                    return
                subj = srow.geometry
                if subj.geom_type not in ("Polygon", "MultiPolygon"):
                    continue
                peri = float(subj.boundary.length)
                if peri < 12 or peri > 80:
                    continue
                limit = max(90.0, peri * 2.8)
                if peri >= limit * 0.55:
                    continue
                local, _, _ = localize([subj])
                factor = (limit + 2.0) / peri
                gt = affinity.scale(local[0], xfact=factor, yfact=factor, origin=local[0].centroid)
                if float(gt.boundary.length) < limit:
                    continue
                self.add(
                    "M12", operation="scale",
                    instruction=f"Scale {_noun('area', srow)} F0 so perimeter is at least {limit:.0f} m (now {peri:.1f} m) (Paris {srow['fid']}).",
                    constraints=self._c("M12", type="metric", metric="perimeter", op=">=", value=limit, unit="m"),
                    features=[feature_record("F0", local[0], srow["semantics"], srow["fid"])],
                    gt=gt, rel_after={"perimeter_m": float(gt.boundary.length)}, extra=self._pair_extra(pair),
                )

        self._fill_slots("M12", [
            ("building", 5, lambda w: mine(self.bdf, w, "building")),
            ("zone", 5, lambda w: mine(self.gdf if len(self.gdf) else self.bdf, w, "zone")),
        ])

    def gen_M14(self) -> None:
        self._fill_slots("M14", [
            ("point-point", 3, lambda w: self._mine_distance_band("M14", "within_distance", "point", "point", w, 6, 16, 35, 100)),
            ("point-area", 3, lambda w: self._mine_distance_band("M14", "within_distance", "point", "area", w, 6, 16, 35, 100)),
            ("line-area", 2, lambda w: self._mine_distance_band("M14", "within_distance", "line", "area", w, 6, 16, 35, 100)),
            ("area-area", 2, lambda w: self._mine_distance_band("M14", "within_distance", "area", "area", w, 6, 16, 35, 100)),
        ])

    def gen_M15(self) -> None:
        def mine(k1: str, want: int) -> None:
            pair = f"point-{k1}"
            df1 = self._df(k1)
            sindex = df1.sindex
            pts = self.pdf
            ps = pts.sindex
            for _, ref_row in self._shuffled(df1):
                if self.full("M15") or self._pair_n("M15", pair) >= want:
                    return
                f1 = ref_row.geometry
                if not self._ok_geom(k1, f1):
                    continue
                cands = []
                for j in list(ps.intersection(f1.centroid.buffer(70).bounds))[:12]:
                    row = pts.iloc[j]
                    if row["fid"] == ref_row["fid"]:
                        continue
                    cands.append(row)
                if len(cands) < 3:
                    continue
                cands.sort(key=lambda r: float(f1.distance(r.geometry)))
                near = cands[:2]
                d_near = min(float(f1.distance(r.geometry)) for r in near)
                if d_near < 10 or d_near > 45:
                    continue
                f0_row = None
                for r in cands[2:]:
                    if float(f1.distance(r.geometry)) > d_near + 10:
                        f0_row = r
                        break
                if f0_row is None:
                    continue
                x1, y1 = _xy(f1)
                gt_w = Point(x1 + 3.2, y1 + 1.6)
                if float(gt_w.distance(f1)) + 2.5 >= d_near:
                    continue
                geoms = [f0_row.geometry, f1] + [r.geometry for r in near]
                local, ox, oy = localize(geoms)
                gt = Point(gt_w.x - ox, gt_w.y - oy)
                self.add(
                    "M15", operation="translate",
                    instruction=f"Move POI F0 so it becomes the unique nearest neighbor of {_noun(k1, ref_row)} F1 (Paris {ref_row['fid']}).",
                    constraints=self._c("M15", type="metric", metric="nearest_neighbor", reference="F1", min_value=1.5, margin=2.0, require_disjoint=True),
                    features=self.pack(geoms, [f0_row, ref_row] + near, ["F0", "F1", "C0", "C1"]),
                    gt=gt, rel_after={"nearest": "F0"}, extra=self._pair_extra(pair),
                )

        self._fill_slots("M15", [
            ("point-point", 4, lambda w: mine("point", w)),
            ("point-area", 3, lambda w: mine("area", w)),
            ("point-line", 3, lambda w: mine("line", w)),
        ])

    def gen_M18(self) -> None:
        def mine(src, want: int, pair: str) -> None:
            pts = self.pdf
            p_sindex = pts.sindex
            rows = [row for _, row in src.iterrows() if row.geometry.area >= 80]
            self.rng.shuffle(rows)
            for grow in rows:
                if self.full("M18") or self._pair_n("M18", pair) >= want:
                    return
                region = grow.geometry
                inside, outside = [], []
                for j in list(p_sindex.intersection(region.bounds)):
                    row = pts.iloc[j]
                    g = row.geometry
                    if region.contains(g):
                        inside.append(row)
                    elif float(g.distance(region)) > 20:
                        outside.append(row)
                    if len(inside) >= 2 and len(outside) >= 1:
                        break
                if len(inside) < 2:
                    continue
                if not outside:
                    for _, row in self._shuffled(pts):
                        if float(row.geometry.distance(region)) > 25:
                            outside.append(row)
                            break
                if not outside:
                    continue
                rp = region.representative_point()
                if not region.contains(rp):
                    continue
                f0_row, c0, c1 = outside[0], inside[0], inside[1]
                geoms = [f0_row.geometry, region, c0.geometry, c1.geometry]
                local, ox, oy = localize(geoms)
                gt = Point(rp.x - ox, rp.y - oy)
                self.add(
                    "M18", operation="translate",
                    instruction=f"Move POI F0 into {_noun('area', grow)} F1 so at least 3 POIs lie strictly inside (Paris {grow['fid']}).",
                    constraints=self._c("M18", type="metric", metric="poi_density", reference="F1", region="F1", min_count=3, op=">=", value=3, strict_inside=True, require_subject_inside=True),
                    features=self.pack(geoms, [f0_row, grow, c0, c1], ["F0", "F1", "C0", "C1"]),
                    gt=gt, rel_after={"poi_count": 3}, extra=self._pair_extra(pair),
                )

        self._fill_slots("M18", [
            ("zone", 5, lambda w: mine(self.gdf, w, "zone")),
            ("building", 5, lambda w: mine(self.bdf, w, "building")),
        ])

    # ------------------------------------------------------------------ order
    def gen_O01(self) -> None:
        def mine(k0: str, want: int) -> None:
            pair = f"{k0}-line"
            df0 = self._df(k0)
            sindex = df0.sindex
            for _, rrow in self._shuffled(self.rdf):
                if self.full("O01") or self._pair_n("O01", pair) >= want:
                    return
                line = rrow.geometry
                if line.length < 40:
                    continue
                mid = line.length * 0.4
                after_cut = line.length * 0.68
                for j in list(sindex.intersection(line.buffer(60).bounds))[:25]:
                    srow = df0.iloc[j]
                    f0 = srow.geometry
                    if not self._ok_geom(k0, f0) or float(f0.distance(line)) > 50:
                        continue
                    if line.project(f0.centroid) < after_cut:
                        continue
                    before = line.interpolate(0.22, normalized=True)
                    nx, ny = _line_normal(line)
                    tx, ty = before.x + nx * 7.0, before.y + ny * 7.0
                    if line.project(Point(tx, ty)) >= mid:
                        continue
                    local, ox, oy = localize([f0, line])
                    gt = _move_centroid(local[0], tx - ox, ty - oy)
                    if self.add(
                        "O01", operation="translate",
                        instruction=f"Move {_noun(k0, srow)} F0 from the later half of road F1 to an earlier position (Paris {rrow['fid']}).",
                        constraints=self._c("O01", type="order", relation="before_after", reference="F1", reference_line="F1", value="before", before_frac=0.4),
                        features=self.pack([f0, line], [srow, rrow], ["F0", "F1"]),
                        gt=gt, rel_after={"order": "before"}, extra=self._pair_extra(pair),
                    ):
                        break

        self._fill_slots("O01", [
            ("point-line", 5, lambda w: mine("point", w)),
            ("area-line", 5, lambda w: mine("area", w)),
        ])

    def gen_O02(self) -> None:
        def mine(k0: str, want: int) -> None:
            pair = f"{k0}-line"
            pts = self.pdf
            p_sindex = pts.sindex
            df0 = self._df(k0)
            for _, rrow in self._shuffled(self.rdf):
                if self.full("O02") or self._pair_n("O02", pair) >= want:
                    return
                line = rrow.geometry
                if line.length < 45:
                    continue
                near_pts = []
                for j in list(p_sindex.intersection(line.buffer(40).bounds))[:30]:
                    row = pts.iloc[j]
                    if float(row.geometry.distance(line)) < 35:
                        near_pts.append(row)
                if len(near_pts) < 1:
                    continue
                near_pts.sort(key=lambda r: line.project(r.geometry))
                f2_row = near_pts[len(near_pts) // 2]
                p2 = line.project(f2_row.geometry)
                if p2 < line.length * 0.35 or p2 > line.length * 0.75:
                    continue
                sindex0 = df0.sindex
                f0_row = None
                for j in list(sindex0.intersection(line.buffer(50).bounds))[:25]:
                    row = df0.iloc[j]
                    if row["fid"] == f2_row["fid"] or not self._ok_geom(k0, row.geometry):
                        continue
                    if line.project(row.geometry.centroid) > p2 + 12:
                        f0_row = row
                        break
                if f0_row is None:
                    continue
                before = line.interpolate(max(2.0, p2 - 16.0))
                nx, ny = _line_normal(line)
                tx, ty = before.x + nx * 6.0, before.y + ny * 6.0
                geoms = [f0_row.geometry, line, f2_row.geometry]
                local, ox, oy = localize(geoms)
                gt = _move_centroid(local[0], tx - ox, ty - oy)
                self.add(
                    "O02", operation="translate",
                    instruction=f"Move {_noun(k0, f0_row)} F0 so it is before F2 along road F1 (Paris {rrow['fid']}).",
                    constraints=self._c("O02", type="order", relation="along_before", other="F2", reference="F1", reference_line="F1", value="before", margin=6),
                    features=self.pack(geoms, [f0_row, rrow, f2_row], ["F0", "F1", "F2"]),
                    gt=gt, rel_after={"before": "F0", "after": "F2"}, extra=self._pair_extra(pair),
                )

        self._fill_slots("O02", [
            ("point-line", 5, lambda w: mine("point", w)),
            ("area-line", 5, lambda w: mine("area", w)),
        ])

    def gen_O03(self) -> None:
        def mine(k0: str, want: int) -> None:
            pair = f"{k0}-line"
            df0 = self._df(k0)
            sindex = df0.sindex
            for i, rrow in self._shuffled(self.rdf):
                if self.full("O03") or self._pair_n("O03", pair) >= want:
                    return
                line = rrow.geometry
                if line.length < 20:
                    continue
                val = "same_side" if i % 2 == 0 else "opposite_side"
                near = []
                for j in list(sindex.intersection(line.buffer(50).bounds))[:30]:
                    row = df0.iloc[j]
                    if not self._ok_geom(k0, row.geometry):
                        continue
                    if 6 < float(row.geometry.distance(line)) < 40 and _line_side(line, row.geometry) != "on":
                        near.append(row)
                if len(near) < 2:
                    continue
                f2_row = near[0]
                f2_side = _line_side(line, f2_row.geometry)
                f0_row = None
                for r in near[1:]:
                    s = _line_side(line, r.geometry)
                    cur = "same_side" if s == f2_side else "opposite_side"
                    if cur != val:
                        f0_row = r
                        break
                if f0_row is None:
                    continue
                nx, ny = _line_normal(line)
                mid = line.interpolate(0.5, normalized=True)
                sign = 1.0 if f2_side == "left" else -1.0
                if val == "opposite_side":
                    sign = -sign
                tx, ty = mid.x + sign * nx * 11.0, mid.y + sign * ny * 11.0
                geoms = [f0_row.geometry, line, f2_row.geometry]
                local, ox, oy = localize(geoms)
                gt = _move_centroid(local[0], tx - ox, ty - oy)
                self.add(
                    "O03", operation="translate",
                    instruction=f"Move {_noun(k0, f0_row)} F0 to the {val} of F2 relative to road F1 (Paris {rrow['fid']}).",
                    constraints=self._c("O03", type="order", relation="same_opposite_side", other="F2", reference="F1", value=val),
                    features=self.pack(geoms, [f0_row, rrow, f2_row], ["F0", "F1", "F2"]),
                    gt=gt, rel_after={"relation": val}, extra=self._pair_extra(pair),
                )

        self._fill_slots("O03", [
            ("point-line", 5, lambda w: mine("point", w)),
            ("area-line", 5, lambda w: mine("area", w)),
        ])

    def gen_O04(self) -> None:
        def mine(k: str, want: int) -> None:
            pair = f"{k}-{k}"
            df = self._df(k)
            n = len(df)
            for i, arow in self._shuffled(df):
                if self.full("O04") or self._pair_n("O04", pair) >= want:
                    return
                a = arow.geometry
                if not self._ok_geom(k, a):
                    continue
                ax = _xy(a)[0]
                for t in range(2, min(40, n)):
                    crow = df.iloc[(i + 5 * t) % n]
                    c = crow.geometry
                    if not self._ok_geom(k, c):
                        continue
                    cx = _xy(c)[0]
                    span = abs(ax - cx)
                    if span < 45 or span > 180:
                        continue
                    lo, hi = sorted([ax, cx])
                    f0_row = None
                    for u in range(1, min(30, n)):
                        prow = df.iloc[(i + 3 * u) % n]
                        if prow["fid"] in (arow["fid"], crow["fid"]) or not self._ok_geom(k, prow.geometry):
                            continue
                        x = _xy(prow.geometry)[0]
                        if x < lo - 8 or x > hi + 8:
                            f0_row = prow
                            break
                    if f0_row is None:
                        continue
                    mx, my = (ax + cx) / 2.0, (_xy(a)[1] + _xy(c)[1]) / 2.0
                    local, ox, oy = localize([f0_row.geometry, a, c])
                    gt = _move_centroid(local[0], mx - ox, my - oy)
                    if self.add(
                        "O04", operation="translate",
                        instruction=f"Move {_noun(k, f0_row)} F0 so it lies strictly between F1 and F2 in x (Paris {f0_row['fid']}).",
                        constraints=self._c("O04", type="order", relation="between", bound_a="F1", bound_b="F2", margin=4),
                        features=self.pack([f0_row.geometry, a, c], [f0_row, arow, crow], ["F0", "F1", "F2"]),
                        gt=gt, rel_after={"between": True}, extra=self._pair_extra(pair),
                    ):
                        break

        def mine_line(want: int) -> None:
            pair = "line-line"
            df = self.rdf
            n = len(df)
            for i, arow in self._shuffled(df):
                if self.full("O04") or self._pair_n("O04", pair) >= want:
                    return
                a = arow.geometry
                if not self._ok_geom("line", a):
                    continue
                ax = _xy(a)[0]
                for t in range(2, min(40, n)):
                    crow = df.iloc[(i + 5 * t) % n]
                    c = crow.geometry
                    if not self._ok_geom("line", c):
                        continue
                    cx = _xy(c)[0]
                    span = abs(ax - cx)
                    if span < 45 or span > 220:
                        continue
                    lo, hi = sorted([ax, cx])
                    f0_row = None
                    for u in range(1, min(30, n)):
                        lrow = df.iloc[(i + 3 * u) % n]
                        if lrow["fid"] in (arow["fid"], crow["fid"]) or not self._ok_geom("line", lrow.geometry):
                            continue
                        x = _xy(lrow.geometry)[0]
                        if x < lo - 8 or x > hi + 8:
                            f0_row = lrow
                            break
                    if f0_row is None:
                        continue
                    mx, my = (ax + cx) / 2.0, (_xy(a)[1] + _xy(c)[1]) / 2.0
                    local, ox, oy = localize([f0_row.geometry, a, c])
                    gt = _move_centroid(local[0], mx - ox, my - oy)
                    if self.add(
                        "O04", operation="translate",
                        instruction=f"Move road F0 so it lies strictly between F1 and F2 in x (Paris {f0_row['fid']}).",
                        constraints=self._c("O04", type="order", relation="between", bound_a="F1", bound_b="F2", margin=4),
                        features=self.pack([f0_row.geometry, a, c], [f0_row, arow, crow], ["F0", "F1", "F2"]),
                        gt=gt, rel_after={"between": True}, extra=self._pair_extra(pair),
                    ):
                        break

        self._fill_slots("O04", [
            ("point-point", 9, lambda w: mine("point", w)),
            ("area-area", 8, lambda w: mine("area", w)),
            ("line-line", 8, lambda w: mine_line(w)),
        ])

    def gen_O06(self) -> None:
        for i, rrow in self._shuffled(self.rdf):
            if self.full("O06"):
                return
            other = self.rdf.iloc[(i + 1) % len(self.rdf)]
            if other["fid"] == rrow["fid"]:
                continue
            local, _, _ = localize([rrow.geometry, other.geometry])
            f0f = feature_record("F0", local[0], rrow["semantics"], rrow["fid"])
            f1f = feature_record("F1", local[1], other["semantics"], other["fid"])
            f0f["semantics"]["layer"] = 0
            f1f["semantics"]["layer"] = 1
            self.add(
                "O06", operation="attribute_edit",
                instruction=f"Edit F0's layer so it sits above F1 (F0 starts at layer 0, F1 is layer 1). Geometry may stay (Paris {rrow['fid']}).",
                constraints=self._c("O06", type="order", relation="elevation_order", reference="F1", value="above"),
                features=[f0f, f1f], gt=local[0], rel_after={"vertical": "above"},
                extra={**self._pair_extra("line-line"), "context": {"F0_layer": 0, "F1_layer": 1, "predicted_F0_layer": 2}},
            )

    def gen_O08(self) -> None:
        n = len(self.rdf)
        for i, rrow in self._shuffled(self.rdf):
            if self.full("O08"):
                return
            f1 = rrow.geometry
            if not self._ok_geom("line", f1):
                continue
            end = Point(f1.coords[-1])
            for k in range(1, 40):
                orow = self.rdf.iloc[(i + 9 * k) % n]
                if orow["fid"] == rrow["fid"]:
                    continue
                f0 = orow.geometry
                if not self._ok_geom("line", f0) or not f0.disjoint(f1) or float(f0.distance(f1)) < 15:
                    continue
                start0 = Point(f0.coords[0])
                dx, dy = end.x - start0.x, end.y - start0.y
                connected = affinity.translate(f0, xoff=dx, yoff=dy)
                if not connected.touches(f1) or connected.equals(f1) or connected.overlaps(f1):
                    continue
                local, _, _ = localize([f0, f1])
                gt = affinity.translate(local[0], xoff=dx, yoff=dy)
                if self.add(
                    "O08", operation="translate",
                    instruction=f"Move segment F0 so it touches F1 at an endpoint (Paris {orow['fid']},{rrow['fid']}).",
                    constraints=self._c("O08", type="order", relation="connectivity_transitivity", reference="F1", mode="touches"),
                    features=self.pack([f0, f1], [orow, rrow], ["F0", "F1"]),
                    gt=gt, rel_after={"connects": True}, extra=self._pair_extra("line-line"),
                ):
                    break

    def run_all(self) -> None:
        for rid in CORE_28_IDS:
            fn = getattr(self, f"gen_{rid}")
            print(f"  mining {rid}...", flush=True)
            fn()
            pairs = Counter(t.get("geom_pair", "?") for t in self.tasks if t["relation_id"] == rid)
            print(f"    -> {self.counts.get(rid, 0)}  {dict(pairs)}", flush=True)

