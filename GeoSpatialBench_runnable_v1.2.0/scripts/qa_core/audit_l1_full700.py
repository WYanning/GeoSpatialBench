#!/usr/bin/env python3
"""Independent structural, geometry, oracle, prompt-fidelity and leakage audit."""

from __future__ import annotations

import argparse
import importlib.util
import json
from collections import Counter
from pathlib import Path

from shapely.geometry import shape
from shapely.wkt import loads as load_wkt


ROOT = Path(__file__).resolve().parents[2]
MODES = ["wkt_exact", "geojson_centroid_bbox", "geojson_exact", "geojson_exact_attributes"]
ACTIVE = ["T01","T02","T03","T04","T05","T06","T08","T09","T11","D01","D02","D04","D05","D06","M01","M02","M04","M08","M12","M14","M15","M18","O01","O02","O03","O04","O06","O08"]


def geometry_kind(geometry_type: str) -> str:
    if "Point" in geometry_type: return "P"
    if "LineString" in geometry_type: return "L"
    if "Polygon" in geometry_type: return "A"
    return "X"


def geometry_signature(item: dict) -> str:
    return "-".join(geometry_kind(o["geometry_type"]) for o in item["objects"])


def import_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


NTA = import_script("nta700", Path(__file__).with_name("audit_non_topology_v1.py"))


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def topology_recompute(item: dict):
    rid = item["relation_id"]
    a, b = (load_wkt(o["geometry_wkt"]) for o in item["objects"][:2])
    if rid == "T01": return a.equals(b)
    if rid == "T02": return a.disjoint(b)
    if rid == "T03": return "yes" if a.intersects(b) else "no"
    if rid == "T04": return a.touches(b)
    if rid == "T05": return a.crosses(b)
    if rid == "T06": return a.contains(b) if item["gold"]["answer"] == "contains" else a.within(b)
    if rid == "T08": return a.overlaps(b)
    if rid == "T09": return a.covers(b)
    if rid == "T11": return "yes" if a.contains(b) and not a.equals(b) else "no"
    raise KeyError(rid)


def payload_from_prompt(text: str) -> dict:
    start = text.index('{"type":"FeatureCollection"')
    markers = [p for p in (text.find("\nAnswer format:", start), text.find("\nOptions:", start)) if p >= 0]
    end = min(markers)
    return json.loads(text[start:end])


def audit(root: Path) -> dict:
    items = read_jsonl(root / "data" / "l1_full700_items_700.jsonl")
    errors, warnings = [], []
    counts = Counter(x["relation_id"] for x in items)
    if len(items) != 700: errors.append(f"expected 700 canonical items, got {len(items)}")
    if list(counts) != ACTIVE: errors.append("relation order/content differs from locked active registry")
    if any(counts[r] != 25 for r in ACTIVE): errors.append(f"not exactly 25 items per relation: {dict(counts)}")
    ids = [x["item_id"] for x in items]
    if len(ids) != len(set(ids)): errors.append("duplicate item IDs")
    if any("difficulty" in x or "difficulty_label" in x for x in items): errors.append("difficulty labels were assigned")
    if any("design_factors" not in x for x in items): errors.append("missing objective design_factors")

    invalid, oracle_fail = [], []
    object_count = 0
    for x in items:
        object_count += len(x["objects"])
        for o in x["objects"]:
            g = load_wkt(o["geometry_wkt"])
            if g.is_empty or not g.is_valid: invalid.append([x["item_id"], o["id"]])
        expected = x["gold"]["answer"]
        if x["relation_id"].startswith("T"):
            actual = topology_recompute(x)
            good = actual is True if x["relation_id"] not in {"T03", "T11"} else actual == expected
        else:
            actual = NTA.recompute(x)
            good = abs(float(actual)-float(expected)) <= 1e-6 if isinstance(expected, (int, float)) else actual == expected
        if not good: oracle_fail.append({"item_id": x["item_id"], "expected": expected, "actual": actual})
    if invalid: errors.append(f"invalid/empty geometries: {invalid[:10]}")
    if oracle_fail: errors.append(f"oracle mismatches: {oracle_fail[:10]}")

    prompt_counts, prompt_lengths, mode_fail, leakage = {}, {}, [], []
    forbidden = ("relation_id", "gold_answer", "gold_label", "source_id", "computed_truth", "oracle_operation")
    prompt_tables = {}
    for form in ("open", "mcq4"):
        for mode in MODES:
            rows = read_jsonl(root / "prompts" / f"l1_full700_{form}_{mode}_700.jsonl")
            prompt_tables[(form, mode)] = rows
            prompt_counts[f"{form}:{mode}"] = len(rows)
            prompt_lengths[f"{form}:{mode}"] = round(sum(len(r["messages"][0]["content"]) for r in rows) / max(1, len(rows)), 1)
            if len(rows) != 700: errors.append(f"{form}:{mode} expected 700 prompts, got {len(rows)}")
            for row, source in zip(rows, items):
                text = row["messages"][0]["content"]
                if row["item_id"] != source["item_id"]: mode_fail.append([row["prompt_id"], "item order"])
                if row["question_form"] != form or row["representation_mode"] != mode: mode_fail.append([row["prompt_id"], "metadata"])
                if any(token in text for token in forbidden): leakage.append([row["prompt_id"], "private field"])
                if mode.startswith("geojson"):
                    payload = payload_from_prompt(text)
                    if len(payload["features"]) != len(source["objects"]): mode_fail.append([row["prompt_id"], "feature count"])
                    for ft, obj in zip(payload["features"], source["objects"]):
                        shown, original = shape(ft["geometry"]), load_wkt(obj["geometry_wkt"])
                        if ft["properties"].get("id") != obj["id"]: mode_fail.append([row["prompt_id"], "feature ID"])
                        if mode == "geojson_centroid_bbox":
                            if not shown.equals_exact(original.centroid, 1e-6): mode_fail.append([row["prompt_id"], "centroid"])
                        elif not shown.equals_exact(original, 1e-6): mode_fail.append([row["prompt_id"], "exact geometry"])
                        if mode == "geojson_exact" and set(ft["properties"]) != {"id"}: mode_fail.append([row["prompt_id"], "attribute leakage"])
    if mode_fail: errors.append(f"prompt mode fidelity failures: {mode_fail[:10]}")
    if leakage: errors.append(f"model-facing leakage: {leakage[:10]}")

    expected_combined = {"l1_full700_open_all_modes_2800.jsonl": 2800, "l1_full700_mcq4_all_modes_2800.jsonl": 2800, "l1_full700_two_forms_all_5600.jsonl": 5600}
    combined_counts = {}
    for name, expected in expected_combined.items():
        n = len(read_jsonl(root / "prompts" / name)); combined_counts[name] = n
        if n != expected: errors.append(f"{name}: expected {expected}, got {n}")

    open_gold = read_jsonl(root / "preds" / "l1_full700_open_private_gold_700.jsonl")
    mcq_gold = read_jsonl(root / "preds" / "l1_full700_mcq4_private_gold_700.jsonl")
    if len(open_gold) != 700 or len(mcq_gold) != 700: errors.append("private gold length mismatch")
    letter_counts = Counter(x["answer_letter"] for x in mcq_gold)
    if letter_counts != Counter({"A":175,"B":175,"C":175,"D":175}): errors.append(f"MCQ letter imbalance: {dict(letter_counts)}")
    if any(len(x["options"]) != 4 or len(set(x["options"])) != 4 for x in mcq_gold): errors.append("MCQ options are not four unique choices")
    for mode in MODES[1:]:
        base = prompt_tables[("mcq4", MODES[0])]
        other = prompt_tables[("mcq4", mode)]
        for a, b in zip(base, other):
            ao = a["messages"][0]["content"].split("Options:\n",1)[1].split("\nReturn only",1)[0]
            bo = b["messages"][0]["content"].split("Options:\n",1)[1].split("\nReturn only",1)[0]
            if ao != bo: errors.append(f"MCQ options changed across modes: {a['item_id']}"); break

    balance = {r: Counter(str(x["gold"]["answer"]) for x in items if x["relation_id"] == r) for r in ACTIVE}
    for rid in ("T03", "T11", "M14", "O04", "O08"):
        if len(balance[rid]) < 2: warnings.append(f"{rid} lacks answer-class balance")
    if balance["T03"] != Counter({"yes":13,"no":12}): errors.append(f"T03 must be balanced 13/12: {dict(balance['T03'])}")
    if balance["T11"] != Counter({"yes":13,"no":12}): errors.append(f"T11 must be balanced 13/12: {dict(balance['T11'])}")
    o06 = [x for x in items if x["relation_id"] == "O06"]
    decision_checks = {
        "T03_independent_yes_no_query": set(balance["T03"]) <= {"yes", "no"},
        "T11_inherited_definition": all(x["gold"]["oracle"].get("definition") == "contains(A,B) and not equals(A,B)" for x in items if x["relation_id"] == "T11"),
        "M02_polygon_polygon_only": all(all("Polygon" in o["geometry_type"] for o in x["objects"]) for x in items if x["relation_id"] == "M02"),
        "M12_has_hole_cases": any(x["gold"]["oracle"]["interior_ring_count"] > 0 for x in items if x["relation_id"] == "M12"),
        "M15_exactly_four_candidates": all(sum(o["role"] == "candidate" for o in x["objects"]) == 4 for x in items if x["relation_id"] == "M15"),
        "M18_exactly_ten_explicit_POIs": all(sum(o["role"] == "explicit_poi" for o in x["objects"]) == 10 for x in items if x["relation_id"] == "M18"),
        "O06_attribute_only": all(not x["mode_information_sufficient"]["geojson_exact"] and x["mode_information_sufficient"]["geojson_exact_attributes"] for x in o06),
    }
    if not all(decision_checks.values()): errors.append(f"locked design decision failed: {decision_checks}")

    # v5.0.1 uses relation-aware quotas instead of forcing a global 1:1:1 ratio.
    expected_quotas = {
        "D01": Counter({"P-P":9,"P-A":4,"A-P":4,"A-A":8}),
        "D02": Counter({"P-P":9,"P-A":4,"A-P":4,"A-A":8}),
        "D04": Counter({"P-P":9,"P-A":4,"A-P":4,"A-A":8}),
        "M01": Counter({"P-P":5,"P-L":4,"P-A":4,"L-L":4,"L-A":4,"A-A":4}),
        "M14": Counter({"P-P":5,"P-L":4,"P-A":4,"L-L":4,"L-A":4,"A-A":4}),
    }
    actual_quotas = {rid:Counter(geometry_signature(x) for x in items if x["relation_id"]==rid) for rid in expected_quotas}
    quota_fail = {rid:{"expected":dict(expected_quotas[rid]),"actual":dict(actual_quotas[rid])} for rid in expected_quotas if actual_quotas[rid]!=expected_quotas[rid]}
    if quota_fail: errors.append(f"v5.0.1 geometry-signature quotas failed: {quota_fail}")
    expected_direction_classes={
        "D01":Counter({"N":7,"E":6,"S":6,"W":6}),
        "D02":Counter({"N":4,"NE":3,"E":3,"SE":3,"S":3,"SW":3,"W":3,"NW":3}),
        "D04":Counter({"N":4,"NE":3,"E":3,"SE":3,"S":3,"SW":3,"W":3,"NW":3}),
    }
    actual_direction_classes={rid:Counter(x["design_factors"].get("direction_class") for x in items if x["relation_id"]==rid) for rid in expected_direction_classes}
    direction_fail={rid:{"expected":dict(expected_direction_classes[rid]),"actual":dict(actual_direction_classes[rid])} for rid in expected_direction_classes if actual_direction_classes[rid]!=expected_direction_classes[rid]}
    if direction_fail: errors.append(f"direction class quotas failed: {direction_fail}")

    object_counts=Counter(); item_presence=Counter(); composition=Counter()
    for x in items:
        kinds=[geometry_kind(o["geometry_type"]) for o in x["objects"]]
        object_counts.update(kinds); item_presence.update(set(kinds)); composition["mixed" if len(set(kinds))>1 else {"P":"point_only","L":"line_only","A":"area_only"}.get(kinds[0],"unknown")]+=1
    expected_objects=Counter({"P":946,"L":534,"A":419})
    expected_presence=Counter({"P":373,"L":330,"A":303})
    expected_composition=Counter({"point_only":123,"line_only":130,"area_only":141,"mixed":306})
    if object_counts!=expected_objects: errors.append(f"global geometry object counts changed: expected {dict(expected_objects)}, got {dict(object_counts)}")
    if item_presence!=expected_presence: errors.append(f"item geometry exposure changed: expected {dict(expected_presence)}, got {dict(item_presence)}")
    if composition!=expected_composition: errors.append(f"item composition changed: expected {dict(expected_composition)}, got {dict(composition)}")
    exposure_total=sum(item_presence.values()); object_total=sum(object_counts.values())
    geometry_balance={
        "signature_quotas":{rid:dict(actual_quotas[rid]) for rid in actual_quotas},
        "direction_class_quotas":{rid:dict(actual_direction_classes[rid]) for rid in actual_direction_classes},
        "object_counts":dict(object_counts),
        "object_percent":{k:round(100*v/object_total,2) for k,v in object_counts.items()},
        "items_containing_type":dict(item_presence),
        "exposure_normalized_percent":{k:round(100*v/exposure_total,2) for k,v in item_presence.items()},
        "item_composition":dict(composition),
    }

    return {
        "status": "PASS" if not errors else "FAIL", "errors": errors, "warnings": warnings,
        "counts": {"canonical_items": len(items), "relations": len(counts), "by_relation": dict(counts), "prompt_files": prompt_counts, "combined": combined_counts},
        "geometry": {"objects_checked": object_count, "invalid": invalid},
        "oracle": {"items_checked": len(items), "mismatches": oracle_fail},
        "prompt_fidelity": {"failures": len(mode_fail), "average_characters": prompt_lengths},
        "leakage": {"forbidden_hits": len(leakage)}, "mcq_correct_letter_counts": dict(letter_counts),
        "answer_balance": {r: dict(c) for r, c in balance.items()}, "decision_checks": decision_checks,
        "geometry_balance": geometry_balance,
    }


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--root", type=Path, default=ROOT); args = ap.parse_args()
    report = audit(args.root)
    path = args.root / "reports" / "l1_full700_audit.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "errors": report["errors"], "warnings": report["warnings"]}, ensure_ascii=False))
    raise SystemExit(0 if report["status"] == "PASS" else 1)


if __name__ == "__main__": main()
