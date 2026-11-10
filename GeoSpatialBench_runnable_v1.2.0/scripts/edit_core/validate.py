"""Shapely-only validators for v5 formal-28 edit tasks."""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from shapely import wkt as shapely_wkt
from shapely.geometry.base import BaseGeometry

CARDINAL_4 = ["N", "E", "S", "W"]
CARDINAL_8 = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]
CARDINAL_16 = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]


def _bearing_deg(from_pt: BaseGeometry, to_pt: BaseGeometry) -> float:
    dx = to_pt.centroid.x - from_pt.centroid.x
    dy = to_pt.centroid.y - from_pt.centroid.y
    return math.degrees(math.atan2(dx, dy)) % 360.0


def _cardinal(deg: float, n: int) -> str:
    labels = {4: CARDINAL_4, 8: CARDINAL_8, 16: CARDINAL_16}[n]
    sector = 360.0 / n
    idx = int((deg + sector / 2.0) // sector) % n
    return labels[idx]


def _scene_map(features: List[Dict[str, Any]]) -> Dict[str, BaseGeometry]:
    out: Dict[str, BaseGeometry] = {}
    for feat in features:
        geom = feat.get("geometry")
        if geom is None:
            geom = shapely_wkt.loads(feat["wkt"])
        out[feat["feature_id"]] = geom
    return out


def _compare(op: str, got: float, limit: float) -> bool:
    if op == "<=":
        return got <= limit + 1e-6
    if op == "<":
        return got < limit
    if op == ">=":
        return got + 1e-6 >= limit
    if op == ">":
        return got > limit
    if op in ("==", "="):
        return abs(got - limit) <= 1e-4
    return False


def _coord2(c) -> Tuple[float, float]:
    return float(c[0]), float(c[1])


def _line_side(line: BaseGeometry, pt: BaseGeometry) -> str:
    coords = list(line.coords)
    ax, ay = _coord2(coords[0])
    bx, by = _coord2(coords[-1])
    px, py = pt.centroid.x, pt.centroid.y
    cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
    if abs(cross) < 1e-9:
        return "on"
    return "left" if cross > 0 else "right"


def _topo_pred(rel: str, subject: BaseGeometry, ref: BaseGeometry) -> bool:
    if rel == "equals":
        return bool(subject.equals(ref))
    if rel == "disjoint":
        return bool(subject.disjoint(ref))
    if rel == "intersects":
        return bool(subject.intersects(ref))
    if rel == "touches":
        return bool(subject.touches(ref))
    if rel == "crosses":
        return bool(subject.crosses(ref))
    if rel == "contains":
        return bool(subject.contains(ref))
    if rel == "within":
        return bool(subject.within(ref))
    if rel == "overlaps":
        return bool(subject.overlaps(ref))
    if rel == "covers":
        return bool(subject.covers(ref))
    if rel == "contains_properly":
        try:
            return bool(ref.contains_properly(subject) or subject.contains_properly(ref))
        except Exception:
            return bool(
                (ref.contains(subject) and not ref.touches(subject))
                or (subject.contains(ref) and not subject.touches(ref))
            )
    return False



def _almost_touches(a: BaseGeometry, b: BaseGeometry, tol: float) -> bool:
    """Match the release editv2 tolerance policy for gaps and shallow overlap."""
    if a.equals(b):
        return False
    if a.touches(b):
        return True
    distance = float(a.distance(b))
    if distance > tol + 1e-6:
        return False
    if distance > 0:
        return True
    if ((a.contains(b) and not a.touches(b))
            or (b.contains(a) and not b.touches(a))):
        return False
    intersection = a.intersection(b)
    if intersection is None or intersection.is_empty:
        return True
    try:
        return bool(intersection.buffer(-tol).is_empty)
    except Exception:
        return False


def _line_connects(subject: BaseGeometry, ref: BaseGeometry, tol: float) -> bool:
    """Match release editv2: endpoint contact or a disjoint gap within tolerance."""
    from shapely.geometry import Point

    if subject.geom_type != "LineString" or ref.geom_type != "LineString":
        return False
    if subject.equals(ref):
        return False
    if subject.touches(ref):
        return True
    if any(Point(coord).distance(ref) <= tol + 1e-6
           for coord in (subject.coords[0], subject.coords[-1])):
        return True
    return bool(not subject.intersects(ref) and subject.distance(ref) <= tol + 1e-6)


def check_constraint(
    constraint: Dict[str, Any],
    subject: BaseGeometry,
    scene: Dict[str, BaseGeometry],
    context: Optional[Dict[str, Any]] = None,
) -> Tuple[bool, Dict[str, Any]]:
    ctx = context or {}
    ctype = constraint.get("type")
    rel = (constraint.get("relation") or "").lower()
    metric = constraint.get("metric")
    ref_id = constraint.get("reference") or constraint.get("reference_line")
    ref = scene.get(ref_id) if ref_id else None

    # An explicit displacement contract measures before -> after, not reference -> after.
    # Keep legacy positional/orientation semantics for constraints without this flag.
    if constraint.get("score_on") == "displacement" and (
        rel.startswith("cardinal") or rel in {"bearing", "relative_observer"}
    ):
        before = scene.get(constraint.get("object") or "F0")
        if before is None:
            return False, {"satisfied": False, "reason": "missing_before"}
        dx = subject.centroid.x - before.centroid.x
        dy = subject.centroid.y - before.centroid.y
        move = math.hypot(dx, dy)
        heading = math.degrees(math.atan2(dx, dy)) % 360.0
        detail = {
            "satisfied": False, "score_on": "displacement",
            "dx": dx, "dy": dy, "move_m": move, "got_deg": heading,
        }
        if not math.isfinite(move) or move <= 1e-9:
            return False, {**detail, "reason": "zero_or_invalid_displacement"}
        if move < float(constraint.get("min_move", 0.0)) - 1e-6:
            return False, {**detail, "reason": "move_too_small"}
        if move > float(constraint.get("max_move", math.inf)) + 1e-6:
            return False, {**detail, "reason": "move_too_large"}
        if constraint.get("min_sep") is not None or constraint.get("max_sep") is not None:
            if ref is None:
                return False, {**detail, "reason": "missing_reference"}
            sep_mode = constraint.get("sep_mode") or "centroid"
            if sep_mode not in {"geometry", "centroid"}:
                return False, {**detail, "reason": "unknown_sep_mode"}
            sep = float(subject.distance(ref) if sep_mode == "geometry"
                        else subject.centroid.distance(ref.centroid))
            detail.update(sep_mode=sep_mode, sep_m=sep)
            if constraint.get("min_sep") is not None and sep < float(constraint["min_sep"]) - 1e-6:
                return False, {**detail, "reason": "too_close"}
            if constraint.get("max_sep") is not None and sep > float(constraint["max_sep"]) + 1e-6:
                return False, {**detail, "reason": "too_far"}
        if rel.startswith("cardinal"):
            n = int(rel.rsplit("_", 1)[-1])
            got = _cardinal(heading, n)
            expected = constraint.get("value")
            ok = got == expected
            detail.update(got=got, expected=expected)
        elif rel == "bearing":
            target = float(constraint.get("value_deg", constraint.get("value", 0)))
            tol = float(constraint.get("tolerance_deg", 12.0))
            error = abs((heading - target + 180) % 360 - 180)
            ok = error <= tol + 1e-9
            detail.update(expected_deg=target, tolerance_deg=tol, angular_error_deg=error)
        else:
            if ref is None or ref.geom_type != "LineString":
                return False, {**detail, "reason": "missing_observer_axis"}
            coords = list(ref.coords)
            rx, ry = coords[-1][0] - coords[0][0], coords[-1][1] - coords[0][1]
            if math.hypot(rx, ry) <= 1e-9:
                return False, {**detail, "reason": "degenerate_observer_axis"}
            dot, cross = rx * dx + ry * dy, rx * dy - ry * dx
            if abs(dot) >= abs(cross):
                got = "front" if dot > 0 else "back"
            else:
                got = "left" if cross > 0 else "right"
            expected = (constraint.get("value") or "").lower()
            ok = got == expected
            detail.update(got=got, expected=expected)
        detail["satisfied"] = bool(ok)
        if not ok:
            detail["reason"] = "wrong_direction"
        return bool(ok), detail


    if ctype == "topology" or rel in {
        "equals", "disjoint", "intersects", "touches", "crosses",
        "contains", "overlaps", "covers", "contains_properly",
    }:
        if ref is None:
            return False, {"reason": "missing reference"}
        touch_tol = constraint.get("tolerance_m")
        use_touch_tolerance = rel == "touches" and touch_tol is not None
        if use_touch_tolerance:
            tol = float(touch_tol)
            if not math.isfinite(tol) or tol < 0:
                return False, {"satisfied": False, "reason": "invalid_tolerance"}
            ok = _almost_touches(subject, ref, tol)
        else:
            ok = _topo_pred(rel, subject, ref)
        if ok and rel == "disjoint" and constraint.get("min_distance") is not None:
            ok = float(subject.distance(ref)) >= float(constraint["min_distance"]) - 1e-6
        for banned in constraint.get("must_not") or []:
            # The release tolerance policy accepts shallow overlap, but not equals/containment.
            if use_touch_tolerance and str(banned).lower() == "overlaps":
                continue
            if _topo_pred(str(banned).lower(), subject, ref):
                ok = False
                break
        detail = {"satisfied": ok, "relation": rel}
        if use_touch_tolerance:
            detail.update(tolerance_m=tol, distance_m=float(subject.distance(ref)),
                          strict_touches=bool(subject.touches(ref)),
                          tolerance_policy="release_editv2")
        return ok, detail

    if ctype == "directional" or rel in {
        "cardinal_4", "cardinal_8", "cardinal_16", "bearing",
        "relative_observer", "object_orientation", "projection",
    }:
        min_sep = constraint.get("min_sep")
        if min_sep is not None and ref is not None:
            if float(subject.centroid.distance(ref.centroid)) < float(min_sep) - 1e-6:
                return False, {"satisfied": False, "reason": "too_close"}
        if rel.startswith("cardinal") or constraint.get("relation", "").startswith("cardinal"):
            n = 4 if "4" in rel else (16 if "16" in rel else 8)
            if ref is None:
                return False, {"reason": "missing reference"}
            got = _cardinal(_bearing_deg(ref.centroid, subject.centroid), n)
            ok = got == constraint.get("value")
            return ok, {"satisfied": ok, "got": got}
        if rel == "bearing":
            if ref is None:
                return False, {"reason": "missing reference"}
            got = _bearing_deg(ref.centroid, subject.centroid)
            target = float(constraint.get("value_deg", constraint.get("value", 0)))
            tol = float(constraint.get("tolerance_deg", 12.0))
            ok = abs((got - target + 180) % 360 - 180) <= tol
            return ok, {"satisfied": ok, "got_deg": round(got, 2)}
        if rel == "relative_observer":
            if ref is None or ref.geom_type != "LineString":
                return False, {"reason": "missing observer axis"}
            coords = list(ref.coords)
            from shapely.geometry import Point

            line_b = _bearing_deg(Point(coords[0]), Point(coords[-1]))
            origin = Point(coords[0])
            rel_ang = (_bearing_deg(origin, subject.centroid) - line_b + 180) % 360 - 180
            if abs(rel_ang) <= 45:
                got = "front"
            elif abs(rel_ang) >= 135:
                got = "back"
            elif rel_ang < 0:
                got = "left"
            else:
                got = "right"
            ok = got == (constraint.get("value") or "").lower()
            return ok, {"satisfied": ok, "got": got}
        if rel == "object_orientation":
            if subject.geom_type != "LineString":
                return False, {"reason": "need LineString"}
            coords = list(subject.coords)
            from shapely.geometry import Point

            got = _cardinal(_bearing_deg(Point(coords[0]), Point(coords[-1])), 8)
            ok = got == constraint.get("value")
            return ok, {"satisfied": ok, "got": got}
        if rel == "projection":
            if ref is None:
                return False, {"reason": "missing reference"}
            dx = subject.centroid.x - ref.centroid.x
            dy = subject.centroid.y - ref.centroid.y
            axis_min = float(constraint.get("axis_min", 0.0))
            ew = "east" if dx > axis_min else ("west" if dx < -axis_min else "same")
            ns = "north" if dy > axis_min else ("south" if dy < -axis_min else "same")
            ok = ew == (constraint.get("east_west") or ew) and ns == (constraint.get("north_south") or ns)
            return ok, {"satisfied": ok, "east_west": ew, "north_south": ns}

    if ctype == "metric" or metric:
        op = constraint.get("op", "<=")
        limit = float(constraint.get("value", constraint.get("threshold", 0)))
        if metric == "euclidean_distance":
            if ref is None:
                return False, {"reason": "missing reference"}
            got = float(subject.distance(ref))
        elif metric == "boundary_distance":
            if ref is None:
                return False, {"reason": "missing reference"}
            bsub = subject if subject.geom_type == "Point" else subject.boundary
            bref = ref if ref.geom_type == "Point" else ref.boundary
            got = float(bsub.distance(bref))
        elif metric == "manhattan_distance":
            if ref is None:
                return False, {"reason": "missing reference"}
            got = abs(subject.centroid.x - ref.centroid.x) + abs(subject.centroid.y - ref.centroid.y)
        elif metric == "hausdorff_distance":
            if ref is None:
                return False, {"reason": "missing reference"}
            got = float(subject.hausdorff_distance(ref))
        elif metric == "perimeter":
            got = float(subject.boundary.length)
        elif metric == "within_distance":
            if ref is None:
                return False, {"reason": "missing reference"}
            got = float(subject.distance(ref))
            op = "<="
            limit = float(constraint.get("value", constraint.get("threshold", 0)))
        elif metric == "nearest_neighbor":
            if ref is None:
                return False, {"reason": "missing reference"}
            skip = {constraint.get("object"), constraint.get("reference")}
            others = [g for fid, g in scene.items() if fid not in skip]
            d_ref = float(subject.distance(ref))
            d_oth = min((float(ref.distance(g)) for g in others), default=d_ref + 1)
            margin = float(constraint.get("margin", 1e-6))
            ok = d_ref + margin < d_oth
            if constraint.get("min_value") is not None:
                ok = ok and d_ref + 1e-6 >= float(constraint["min_value"])
            if constraint.get("require_disjoint"):
                ok = ok and bool(subject.disjoint(ref))
            return ok, {"satisfied": ok, "distance_to_ref": round(d_ref, 4)}
        elif metric == "poi_density":
            region_id = constraint.get("region") or constraint.get("reference") or "F1"
            region = scene.get(region_id)
            if region is None or region.area <= 0:
                return False, {"reason": "missing region"}
            skip = {constraint.get("object")}
            strict = bool(constraint.get("strict_inside", False))

            def _in_region(g: BaseGeometry) -> bool:
                if g.geom_type != "Point":
                    return False
                return bool(region.contains(g)) if strict else bool(region.contains(g) or region.covers(g))

            n_poi = sum(1 for fid, g in scene.items() if fid not in skip and _in_region(g))
            if _in_region(subject):
                n_poi += 1
            min_count = int(constraint.get("min_count", constraint.get("value", 1)))
            ok = n_poi >= min_count
            if constraint.get("require_subject_inside"):
                ok = ok and _in_region(subject)
            return ok, {"satisfied": ok, "poi_count": n_poi}
        else:
            return False, {"reason": f"unknown metric {metric}"}
        if constraint.get("require_disjoint") and ref is not None and not subject.disjoint(ref):
            return False, {"satisfied": False, "reason": "not_disjoint", "value": round(got, 4)}
        if constraint.get("min_value") is not None and got < float(constraint["min_value"]) - 1e-6:
            return False, {"satisfied": False, "reason": "below_min", "value": round(got, 4)}
        ok = _compare(op, got, limit)
        return ok, {"satisfied": ok, "value": round(got, 4), "op": op, "limit": limit}

    if ctype == "order" or rel in {
        "before_after", "along_before", "same_opposite_side", "between",
        "elevation_order", "containment_transitivity", "connectivity_transitivity",
        "counterfactual_change",
    }:
        if rel == "before_after":
            line = scene.get(constraint.get("reference_line") or constraint.get("reference"))
            if line is None:
                return False, {"reason": "missing line"}
            proj = line.project(subject.centroid)
            frac = float(constraint.get("before_frac", 0.5))
            got = "before" if proj < line.length * frac else "after"
            ok = got == constraint.get("value", "before")
            return ok, {"satisfied": ok, "got": got}
        if rel == "along_before":
            line = scene.get(constraint.get("reference_line") or constraint.get("reference"))
            other = scene.get(constraint.get("other"))
            if line is None or other is None:
                return False, {"reason": "missing line/other"}
            p0 = line.project(subject.centroid)
            p1 = line.project(other.centroid)
            val = constraint.get("value", "before")
            margin = float(constraint.get("margin", 0.0))
            ok = p0 < p1 - margin if val == "before" else p0 > p1 + margin
            return ok, {"satisfied": ok}
        if rel == "same_opposite_side":
            line = scene.get(constraint.get("reference") or constraint.get("reference_line"))
            other = scene.get(constraint.get("other"))
            if line is None or other is None:
                return False, {"reason": "missing line/other"}
            sa, sb = _line_side(line, subject), _line_side(line, other)
            if sa == "on" or sb == "on":
                got = "on"
            else:
                got = "same_side" if sa == sb else "opposite_side"
            ok = got == constraint.get("value", "same_side")
            return ok, {"satisfied": ok, "got": got}
        if rel == "between":
            a = scene.get(constraint.get("bound_a"))
            b = scene.get(constraint.get("bound_b"))
            if a is None or b is None:
                return False, {"reason": "missing bounds"}
            lo, hi = sorted([a.centroid.x, b.centroid.x])
            margin = float(constraint.get("margin", 0.0))
            ok = lo + margin <= subject.centroid.x <= hi - margin
            return ok, {"satisfied": ok}
        if rel == "elevation_order":
            sub_layer = int(ctx.get("predicted_F0_layer", ctx.get("F0_layer", 0)))
            ref_layer = int(ctx.get("F1_layer", 1))
            ok = sub_layer > ref_layer if constraint.get("value", "above") == "above" else sub_layer < ref_layer
            return ok, {"satisfied": ok, "subject_layer": sub_layer, "reference_layer": ref_layer}
        if rel == "containment_transitivity":
            mid = scene.get(constraint.get("b", "F1"))
            outer = scene.get(constraint.get("c", "F2"))
            if mid is None or outer is None:
                return False, {"reason": "missing chain"}
            ok = bool(subject.within(mid) and mid.within(outer) and not subject.equals(mid))
            return ok, {"satisfied": ok}
        if rel == "connectivity_transitivity":
            ref = scene.get(constraint.get("reference", "F1"))
            if ref is None:
                return False, {"reason": "missing reference"}
            if subject.geom_type != "LineString" or ref.geom_type != "LineString":
                return False, {"reason": "need lines"}
            mode = (constraint.get("mode") or "touches").lower()
            if mode == "touches":
                tolerance = constraint.get("tolerance_m")
                if tolerance is not None:
                    tol = float(tolerance)
                    if not math.isfinite(tol) or tol < 0:
                        return False, {"satisfied": False, "reason": "invalid_tolerance"}
                    ok = _line_connects(subject, ref, tol)
                    return ok, {"satisfied": ok, "tolerance_m": tol,
                                "distance_m": float(subject.distance(ref)),
                                "strict_touches": bool(subject.touches(ref)),
                                "tolerance_policy": "release_editv2"}
                ok = bool(subject.touches(ref))
            else:
                ok = bool(subject.touches(ref) or subject.intersects(ref))
            return ok, {"satisfied": ok}
        if rel == "counterfactual_change":
            ref = scene.get(constraint.get("reference", "F1"))
            if ref is None:
                return False, {"reason": "missing reference"}
            min_d = float(constraint.get("min_distance", 0.0))
            ok = bool(subject.disjoint(ref)) and float(subject.distance(ref)) >= min_d - 1e-6
            return ok, {"satisfied": ok, "expected_change": "disconnect"}

    return False, {"reason": f"unhandled constraint {constraint}"}


def evaluate_edit(
    task: Dict[str, Any],
    prediction_wkt: str,
    extra_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Score an edit prediction. GT is not unique; constraints must hold and geometry must change (except O06)."""
    try:
        pred = shapely_wkt.loads(prediction_wkt)
        parse_ok = pred is not None and not pred.is_empty
    except Exception as exc:
        return {"parse_ok": False, "valid_geometry": False, "constraints_satisfied": False, "score": 0.0, "error": str(exc)}

    if not parse_ok or not pred.is_valid:
        return {"parse_ok": parse_ok, "valid_geometry": False, "constraints_satisfied": False, "score": 0.0}

    scene = _scene_map(task["scene_features"])
    target = task.get("target_feature_id", "F0")
    before = scene.get(target)
    op = (task.get("operation") or "").lower()
    if op != "attribute_edit" and before is not None and before.equals(pred):
        return {
            "parse_ok": True,
            "valid_geometry": True,
            "constraints_satisfied": False,
            "score": 0.0,
            "reason": "unchanged_geometry",
        }

    ctx = {k: v for k, v in (task.get("context") or {}).items()
           if k != "predicted_F0_layer"}
    if extra_context:
        ctx.update(extra_context)

    details = []
    all_ok = True
    for c in task.get("constraints", []):
        ok, detail = check_constraint(c, pred, scene, context=ctx)
        details.append(detail)
        if not ok:
            all_ok = False
    return {
        "parse_ok": True,
        "valid_geometry": True,
        "constraints_satisfied": all_ok,
        "score": 1.0 if all_ok else 0.0,
        "constraint_details": details,
    }
