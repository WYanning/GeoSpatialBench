#!/usr/bin/env python3
"""Build one city/data case from the reusable GeoFM v5 L1 template."""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import random
from collections import Counter
from pathlib import Path

import geopandas as gpd
import pandas as pd
from pyproj import CRS


TEMPLATE_ROOT = Path(__file__).resolve().parents[2]
CORE_PATH = Path(__file__).with_name("build_l1_full700.py")
AUDIT_PATH = Path(__file__).with_name("audit_l1_full700.py")
ROLE_BY_CORE_LAYER = {
    "gis_osm_pois_free": "pois",
    "gis_osm_roads_free": "roads",
    "gis_osm_buildings_a_free": "buildings",
    "gis_osm_landuse_a_free": "landuse",
}
MIN_ROWS = {"pois": 750, "roads": 1250, "buildings": 650, "landuse": 25}
GEOMETRY_TYPES = {
    "pois": {"Point"},
    "roads": {"LineString", "MultiLineString"},
    "buildings": {"Polygon", "MultiPolygon"},
    "landuse": {"Polygon", "MultiPolygon"},
}


def import_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(x, ensure_ascii=False, separators=(",", ":")) + "\n" for x in rows),
        encoding="utf-8",
    )


def resolve_path(value: str, config_path: Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else (config_path.parent / path).resolve()


def validate_config(config: dict, config_path: Path) -> tuple[CRS, Path]:
    required = ("city_name", "id_prefix", "target_crs", "output_dir", "inputs")
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise ValueError(f"Missing configuration keys: {missing}")
    prefix = str(config["id_prefix"])
    if not prefix.replace("-", "").isalnum() or len(prefix) > 12:
        raise ValueError("id_prefix must be <=12 alphanumeric/hyphen characters")
    crs = CRS.from_user_input(config["target_crs"])
    if not crs.is_projected:
        raise ValueError("target_crs must be projected; geographic longitude/latitude is not allowed")
    unit_names = {axis.unit_name.lower() for axis in crs.axis_info if axis.unit_name}
    if not unit_names or not all(name in {"metre", "meter", "metres", "meters"} for name in unit_names):
        raise ValueError(f"target_crs axes must use metres, got {sorted(unit_names)}")
    for role in GEOMETRY_TYPES:
        entry = config["inputs"].get(role)
        if not entry or not entry.get("path"):
            raise ValueError(f"inputs.{role}.path is required")
        source_path = resolve_path(entry["path"], config_path)
        if not source_path.exists():
            raise FileNotFoundError(f"inputs.{role}.path does not exist: {source_path}")
    out = resolve_path(config["output_dir"], config_path)
    return crs, out


def normalized_flag(value) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "F"
    return "T" if str(value).strip().lower() in {"t", "true", "yes", "y", "1"} else "F"


def read_role(role: str, config: dict, config_path: Path, target_crs: CRS) -> gpd.GeoDataFrame:
    entry = config["inputs"][role]
    path = resolve_path(entry["path"], config_path)
    kwargs = {}
    if entry.get("layer") is not None:
        kwargs["layer"] = entry["layer"]
    bbox = entry.get("read_bbox", config.get("read_bbox"))
    if bbox is not None:
        if len(bbox) != 4:
            raise ValueError(f"inputs.{role}.read_bbox must contain four numbers")
        kwargs["bbox"] = tuple(bbox)
    frame = gpd.read_file(path, **kwargs)
    for field, accepted in entry.get("attribute_filter", {}).items():
        if field not in frame.columns or not isinstance(accepted, list):
            raise ValueError(f"inputs.{role}.attribute_filter needs an existing field and a list of accepted values")
        frame = frame[frame[field].isin(accepted)].copy()
    if frame.crs is None:
        source_crs = entry.get("source_crs")
        if not source_crs:
            raise ValueError(f"inputs.{role} has no CRS; set source_crs explicitly")
        frame = frame.set_crs(source_crs)
    frame = frame.dropna(subset=[frame.geometry.name]).explode(index_parts=False).reset_index(drop=True)
    frame = frame[frame.geometry.geom_type.isin(GEOMETRY_TYPES[role])].copy()
    frame = frame[~frame.geometry.is_empty].to_crs(target_crs).reset_index(drop=True)
    invalid = int((~frame.geometry.is_valid).sum())
    if invalid:
        raise ValueError(f"inputs.{role} contains {invalid} invalid geometries after projection; repair the source first")
    frame["_template_row_id"] = frame.index.astype(str)
    id_field = entry.get("id_field")
    if id_field:
        if id_field not in frame.columns:
            raise ValueError(f"inputs.{role}.id_field '{id_field}' is absent")
        frame["_template_row_id"] = frame[id_field].fillna(frame["_template_row_id"]).astype(str)
    frame["_template_source_name"] = entry.get("source_name") or entry.get("layer") or path.name

    if role == "roads":
        field_map = entry.get("field_map", {})
        for canonical in ("layer", "bridge", "tunnel"):
            source = field_map.get(canonical, canonical)
            constant = entry.get("constant_fields", {}).get(canonical)
            if source in frame.columns:
                frame[canonical] = frame[source]
            elif constant is not None:
                frame[canonical] = constant
            elif canonical == "layer":
                raise ValueError("roads need an ordinal layer field for O06; map it with field_map.layer")
            else:
                frame[canonical] = "F"
        parsed_layer = pd.to_numeric(frame["layer"], errors="coerce")
        bad_layer = frame["layer"].notna() & parsed_layer.isna()
        if bad_layer.any():
            raise ValueError("roads.layer contains non-numeric values; provide ordinal numeric layer values")
        frame["layer"] = parsed_layer.fillna(0.0)
        frame["bridge"] = frame["bridge"].map(normalized_flag)
        frame["tunnel"] = frame["tunnel"].map(normalized_flag)

    filters = config.get("filters", {})
    if role == "roads":
        frame["_length"] = frame.length
        lo, hi = filters.get("road_length_m", [30, 1200])
        frame = frame[(frame._length > lo) & (frame._length < hi)]
    elif role in {"buildings", "landuse"}:
        frame["_area"] = frame.area
        key = "building_area_m2" if role == "buildings" else "landuse_area_m2"
        default = [50, 30000] if role == "buildings" else [20000, 3000000]
        lo, hi = filters.get(key, default)
        frame = frame[(frame._area > lo) & (frame._area < hi)]
    random_state = {"pois": 18, "roads": 19, "buildings": 20, "landuse": 21}[role]
    return frame.sample(frac=1, random_state=random_state).reset_index(drop=True)


def data_diagnostics(frames: dict[str, gpd.GeoDataFrame]) -> dict:
    checks = {}
    for role, frame in frames.items():
        checks[role] = {
            "rows_after_filtering": len(frame),
            "minimum_required": MIN_ROWS[role],
            "geometry_types": dict(Counter(frame.geometry.geom_type)),
            "enough_rows": len(frame) >= MIN_ROWS[role],
        }
    roads = frames["roads"]
    checks["roads"]["nonzero_layer_rows"] = int((roads["layer"] != 0).sum())
    checks["roads"]["bridge_or_tunnel_rows"] = int(((roads["bridge"] == "T") | (roads["tunnel"] == "T")).sum())
    return checks


def assert_data_ready(checks: dict) -> None:
    failures = [f"{role}: {value['rows_after_filtering']} < {value['minimum_required']}" for role, value in checks.items() if role in MIN_ROWS and not value["enough_rows"]]
    if checks["roads"]["nonzero_layer_rows"] == 0:
        failures.append("roads: no non-zero layer values; O06 cannot test vertical ordering")
    if failures:
        raise RuntimeError("Input data are not sufficient for the locked 700-item design: " + "; ".join(failures))


def copy_dynamic_registries(target: Path, target_crs: str) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for name in ("l1_active_relation_registry_28.jsonl", "l1_deferred_relations_4.jsonl"):
        rows = [json.loads(line) for line in (TEMPLATE_ROOT / "registry" / name).read_text(encoding="utf-8").splitlines() if line.strip()]
        for row in rows:
            operation = row.get("canonical_operation")
            if isinstance(operation, str):
                row["canonical_operation"] = operation.replace("EPSG:3006", target_crs)
        write_jsonl(target / name, rows)


def patch_core(core, config: dict, frames: dict[str, gpd.GeoDataFrame], target_crs: CRS):
    prefix = str(config["id_prefix"]).upper()
    city = str(config["city_name"])
    crs_text = target_crs.to_string()
    dataset_name = str(config.get("dataset_name", "configured geospatial source layers"))

    def configured_load_data(_):
        return frames["pois"], frames["roads"], frames["buildings"], frames["landuse"]

    def configured_source(row, core_layer):
        role = ROLE_BY_CORE_LAYER[core_layer]
        entry = config["inputs"][role]
        attrs = {}
        for field in entry.get("attribute_fields", []):
            if field in row.index:
                value = row.get(field)
                if value is None or (isinstance(value, float) and math.isnan(value)):
                    continue
                if hasattr(value, "item"):
                    value = value.item()
                attrs[field] = value
        if role == "roads":
            attrs.update({"layer": float(row["layer"]), "bridge": row["bridge"], "tunnel": row["tunnel"]})
        source_name = str(row.get("_template_source_name", role))
        return {
            "source_id": f"{source_name}:{row.get('_template_row_id', 'unknown')}",
            "source_layer": source_name,
            "attributes": attrs,
        }

    original_item = core.item
    def configured_item(rid, idx, objects, task, answer_format, answer, oracle, scoring, construction="observed", factors=None, suff=None):
        result = original_item(rid, idx, objects, task, answer_format, answer, oracle, scoring, construction, factors, suff)
        result["item_id"] = f"{prefix}-L1-{rid}-{idx:03d}"
        result["version"] = "v5.0.1-template"
        result["city"] = city
        result["crs"] = crs_text
        result["provenance"] = {"dataset": dataset_name, "construction": construction}
        return result

    original_prompt_row = core.NT.prompt_row
    def configured_prompt_row(item, mode):
        result = original_prompt_row(item, mode)
        text = result["messages"][0]["content"]
        lines = text.splitlines()
        lines[0] = f"CRS: {crs_text}; coordinate units are metres."
        result["messages"][0]["content"] = "\n".join(lines)
        return result

    core.load_data = configured_load_data
    core.record_source = configured_source
    core.item = configured_item
    core.NT.prompt_row = configured_prompt_row
    core.RNG = random.Random(int(config.get("seed", 20260819)))


def serialization_regression(items: list[dict]) -> dict:
    o04 = [x for x in items if x["relation_id"] == "O04"]
    failures = []
    for item in o04:
        a, b, c = [core_load_wkt(o["geometry_wkt"]) for o in item["objects"]]
        cross = (c.x-a.x)*(b.y-a.y) - (c.y-a.y)*(b.x-a.x)
        dot = (b.x-a.x)*(c.x-a.x) + (b.y-a.y)*(c.y-a.y)
        length2 = (c.x-a.x)**2 + (c.y-a.y)**2
        actual = cross == 0 and 0 < dot < length2
        if actual != (item["gold"]["answer"] == "yes"):
            failures.append(item["item_id"])
    m18 = [x for x in items if x["relation_id"] == "M18"]
    if not all("three decimal places" in x["task"] and x["gold"]["scoring"].get("absolute_tolerance") == .001 for x in m18):
        failures.append("M18-precision-contract")
    return {"status": "PASS" if not failures else "FAIL", "checked_o04": len(o04), "checked_m18": len(m18), "failures": failures}


def core_load_wkt(text: str):
    from shapely.wkt import loads
    return loads(text)


def build(config_path: Path, check_only: bool = False) -> dict:
    config = read_json(config_path)
    target_crs, output_root = validate_config(config, config_path)
    frames = {role: read_role(role, config, config_path, target_crs) for role in GEOMETRY_TYPES}
    checks = data_diagnostics(frames)
    assert_data_ready(checks)
    if check_only:
        return {"status": "PASS", "check_only": True, "output_root": str(output_root), "data": checks}

    core = import_script("v5_template_core", CORE_PATH)
    patch_core(core, config, frames, target_crs)
    pois, roads, buildings, landuse = core.load_data(None)
    items = core.topology_items(pois, roads, buildings) + core.non_top_items(pois, roads, buildings, landuse)
    items.sort(key=lambda x: (core.ACTIVE.index(x["relation_id"]), x["item_id"]))
    core.write_jsonl(output_root / "data" / "l1_full700_items_700.jsonl", items)
    emission = core.emit(items, output_root)
    copy_dynamic_registries(output_root / "registry", target_crs.to_string())
    regression = serialization_regression(items)
    if regression["status"] != "PASS":
        raise RuntimeError(f"serialization regression failed: {regression['failures']}")

    build_report = {
        **emission,
        "canonical_items": len(items),
        "relations": dict(Counter(x["relation_id"] for x in items)),
        "version": "v5.0.1-template",
        "city": config["city_name"],
        "crs": target_crs.to_string(),
        "difficulty_labels_assigned": False,
        "data_diagnostics": checks,
        "serialization_regression": regression,
    }
    write_json(output_root / "reports" / "l1_full700_build_report.json", build_report)
    manifest = {
        "template_version": "5.0.1",
        "city_name": config["city_name"],
        "id_prefix": config["id_prefix"],
        "target_crs": target_crs.to_string(),
        "relation_count": 28,
        "items_per_relation": 25,
        "canonical_items": 700,
        "representation_modes": core.MODES,
        "question_forms": ["open", "mcq4"],
        "model_facing_prompts": 5600,
        "private_files": ["preds/l1_full700_open_private_gold_700.jsonl", "preds/l1_full700_mcq4_private_gold_700.jsonl"],
    }
    write_json(output_root / "manifest.json", manifest)

    audit_module = import_script("v5_template_audit", AUDIT_PATH)
    audit = audit_module.audit(output_root)
    audit["template_serialization_regression"] = regression
    write_json(output_root / "reports" / "l1_full700_audit.json", audit)
    if audit["status"] != "PASS":
        raise RuntimeError("Generated case failed audit: " + "; ".join(audit["errors"]))
    manifest["audit_status"] = audit["status"]
    manifest["geometry_balance"] = audit["geometry_balance"]
    write_json(output_root / "manifest.json", manifest)
    return {"status": "PASS", "output_root": str(output_root), "build": build_report, "audit": {"status": audit["status"], "warnings": audit["warnings"]}}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    result = build(args.config.resolve(), args.check_only)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
