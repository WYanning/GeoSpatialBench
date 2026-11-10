"""Structured editing protocol extracted from the released runner."""
from __future__ import annotations
import json
import re
from typing import Any, Dict, Optional, Tuple
from shapely import affinity
from shapely import wkt as shapely_wkt
from shapely.geometry.base import BaseGeometry
SYSTEM_PROMPT = """You are a spatial geometry editor.
Edit existing feature F0 with a geometric transform. Do NOT output WKT. Do NOT copy F0 coordinates.
A zero/identity transform scores 0 (except attribute_edit).

Output ONLY one JSON object, no markdown, no explanation:
{"op":"translate","dx":<number>,"dy":<number>}
{"op":"rotate","angle_deg":<degrees counterclockwise around F0 centroid>}
{"op":"scale","factor":<positive scale around F0 centroid>}
{"op":"snap","to":"F1"}
{"op":"attribute_edit","layer":2}

dx/dy use the same units as the input GeoJSON coordinates.
Pick the op that matches the task "operation" field when possible.
"""

RETRY_PROMPT = (
    "REJECTED: that output did not change F0 (identity/zero transform or missing JSON). "
    "Output a non-zero transform JSON now. Example: {\"op\":\"translate\",\"dx\":12.5,\"dy\":-8.0}"
)


def iter_jsonl(path: Path) -> Iterator[Dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def extract_json_obj(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    if "</think>" in text:
        text = text.split("</think>")[-1]
    text = text.strip().strip("`")
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    start = text.find("{")
    if start < 0:
        return _loose_dxdy(text)
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                blob = text[start : i + 1]
                for candidate in (blob, blob.replace("'", '"')):
                    try:
                        obj = json.loads(candidate)
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        continue
                return _loose_dxdy(blob)
    return _loose_dxdy(text)


def _loose_dxdy(text: str) -> Optional[Dict[str, Any]]:
    dx = re.search(r"\bdx\b\s*[:=]\s*(-?\d+(?:\.\d+)?)", text, flags=re.I)
    dy = re.search(r"\bdy\b\s*[:=]\s*(-?\d+(?:\.\d+)?)", text, flags=re.I)
    if dx and dy:
        return {"op": "translate", "dx": float(dx.group(1)), "dy": float(dy.group(1))}
    return None


def _f0_and_scene(task: Dict[str, Any]) -> Tuple[Optional[BaseGeometry], Dict[str, BaseGeometry]]:
    scene: Dict[str, BaseGeometry] = {}
    for feat in task["scene_features"]:
        try:
            scene[feat["feature_id"]] = shapely_wkt.loads(feat["wkt"])
        except Exception:
            continue
    return scene.get("F0"), scene


def apply_transform(
    f0: BaseGeometry,
    spec: Dict[str, Any],
    scene: Dict[str, BaseGeometry],
) -> Tuple[Optional[BaseGeometry], Dict[str, Any], str]:
    op = str(spec.get("op") or spec.get("operation") or "").lower().strip()
    if not op:
        if any(k in spec for k in ("dx", "dy", "xoff", "yoff")):
            op = "translate"
        elif any(k in spec for k in ("angle_deg", "angle")):
            op = "rotate"
        elif any(k in spec for k in ("factor", "xfact", "scale")):
            op = "scale"
        elif any(k in spec for k in ("to", "target", "reference")):
            op = "snap"
        elif "layer" in spec:
            op = "attribute_edit"
        else:
            return None, spec, "unknown_op"

    if op in ("translate", "move"):
        dx = float(spec.get("dx", spec.get("xoff", 0)) or 0)
        dy = float(spec.get("dy", spec.get("yoff", 0)) or 0)
        norm = {"op": "translate", "dx": dx, "dy": dy}
        return affinity.translate(f0, xoff=dx, yoff=dy), norm, "ok"
    if op == "rotate":
        ang = float(spec.get("angle_deg", spec.get("angle", 0)) or 0)
        norm = {"op": "rotate", "angle_deg": ang}
        return affinity.rotate(f0, ang, origin=f0.centroid), norm, "ok"
    if op == "scale":
        factor = float(spec.get("factor", spec.get("scale", spec.get("xfact", 1))))
        yfact = float(spec.get("yfact", factor))
        if factor <= 0 or yfact <= 0:
            return None, spec, "nonpositive_scale"
        norm = {"op": "scale", "factor": factor, "yfact": yfact}
        return affinity.scale(f0, xfact=factor, yfact=yfact, origin=f0.centroid), norm, "ok"
    if op == "snap":
        ref_id = str(spec.get("to") or spec.get("target") or spec.get("reference") or "F1")
        ref = scene.get(ref_id)
        if ref is None:
            return None, {"op": "snap", "to": ref_id}, "missing_snap_target"
        return ref, {"op": "snap", "to": ref_id}, "ok"
    if op in ("attribute_edit", "attribute"):
        layer = spec.get("layer")
        if layer is None or isinstance(layer, bool) or not float(layer).is_integer():
            return None, spec, "invalid_layer"
        return f0, {"op": "attribute_edit", "layer": layer}, "ok"
    return None, {"op": op}, f"unsupported_op:{op}"


def is_identity(
    *,
    task_op: str,
    spec: Optional[Dict[str, Any]],
    before: Optional[BaseGeometry],
    after: Optional[BaseGeometry],
) -> bool:
    if (task_op or "").lower() == "attribute_edit":
        return False
    if after is None or before is None:
        return True
    if before.equals(after):
        return True
    if not spec:
        return True
    op = (spec.get("op") or "").lower()
    if op == "translate" and abs(float(spec.get("dx", 0))) + abs(float(spec.get("dy", 0))) < 1e-6:
        return True
    if op == "rotate" and abs(float(spec.get("angle_deg", 0)) % 360.0) < 1e-3:
        return True
    if op == "scale" and abs(float(spec.get("factor", 1)) - 1.0) < 1e-6:
        return True
    return False


def build_model_input(task: Dict[str, Any]) -> str:
    feats = []
    for f in task["scene_features"]:
        feats.append({
            "feature_id": f["feature_id"],
            "geometry_type": f.get("geometry_type"),
            "geojson": f.get("geojson"),
            "centroid": f.get("centroid"),
            "bounding_box": f.get("bounding_box"),
            "area": f.get("area"),
            "length": f.get("length"),
            "semantics": f.get("semantics"),
        })
    payload = {
        "instruction": task["instruction"],
        "operation": task.get("operation"),
        "target_feature_id": "F0",
        "allowed_operations": task.get("allowed_operations"),
        "constraints": task["constraints"],
        "scene_features_geojson": feats,
        "output_feature_id": "F0",
        "output_format": "transform_json",
        "output_schema": {
            "op": "translate|rotate|scale|snap|attribute_edit",
            "dx": "number, translate only",
            "dy": "number, translate only",
            "angle_deg": "number, rotate only",
            "factor": "number, scale only",
            "to": "feature_id, snap only",
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def decode_transform(
    raw: str,
    f0: BaseGeometry,
    scene: Dict[str, BaseGeometry],
) -> Tuple[Optional[BaseGeometry], Optional[Dict[str, Any]], str]:
    spec = extract_json_obj(raw)
    if not spec:
        return None, None, "missing_json"
    geom, norm, status = apply_transform(f0, spec, scene)
    return geom, norm, status

