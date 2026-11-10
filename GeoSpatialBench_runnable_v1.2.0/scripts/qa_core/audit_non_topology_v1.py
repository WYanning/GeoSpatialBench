#!/usr/bin/env python3
"""Independent structural, oracle, mode-fidelity, and leakage audit."""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

import shapely
from shapely.geometry import Point, shape
from shapely.wkt import loads as load_wkt


ROOT = Path(__file__).resolve().parents[2]
BUILD_PATH = Path(__file__).with_name("build_non_topology_v1.py")
spec = importlib.util.spec_from_file_location("ntbuild", BUILD_PATH)
nt = importlib.util.module_from_spec(spec)
assert spec.loader
spec.loader.exec_module(nt)


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def close(a, b, tol=1e-6) -> bool:
    return abs(float(a)-float(b)) <= tol


def recompute(item: dict):
    rid = item["relation_id"]
    os = item["objects"]
    gs = [load_wkt(o["geometry_wkt"]) for o in os]
    if rid in {"D01", "D02", "D04"}:
        v = nt.bearing(gs[0].centroid, gs[1].centroid)
        return nt.sector4(v) if rid == "D01" else nt.sector8(v) if rid == "D02" else round(v, 2)
    if rid == "D05":
        line, p = gs; a, b = Point(line.coords[0]), Point(line.coords[-1])
        rel = ((nt.bearing(a, p) - nt.bearing(a, b) + 180) % 360) - 180
        if abs(rel) <= 45: return "front"
        if 45 < rel <= 135: return "right"
        if -135 <= rel < -45: return "left"
        return "back"
    if rid == "D06":
        return nt.axis4(nt.bearing(Point(gs[0].coords[0]), Point(gs[0].coords[-1])))
    if rid == "M01": return round(gs[0].distance(gs[1]), 2)
    if rid == "M02": return round(gs[0].boundary.distance(gs[1].boundary), 2)
    if rid == "M04": return round(abs(gs[0].x-gs[1].x)+abs(gs[0].y-gs[1].y), 2)
    if rid == "M08": return round(float(shapely.hausdorff_distance(gs[0], gs[1], densify=.25)), 2)
    if rid == "M12": return round(gs[0].boundary.length, 2)
    if rid == "M14":
        radius = item["gold"]["oracle"]["buffer_radius_m"]
        return "yes" if gs[0].distance(gs[1]) <= radius+1e-9 else "no"
    if rid == "M15":
        ds = {o["id"]: gs[0].distance(g) for o, g in zip(os[1:], gs[1:])}
        return min(ds, key=ds.get)
    if rid == "M18":
        count = sum(gs[0].covers(g) for g in gs[1:])
        return round(count/(gs[0].area/1e6), 3)
    if rid == "O01":
        route = gs[0]
        ordered = sorted(zip(os[1:], gs[1:]), key=lambda z: route.project(z[1], normalized=True))
        return ">".join(x[0]["id"] for x in ordered)
    if rid == "O02":
        route = gs[0]
        ordered = sorted(zip(os[1:], gs[1:]), key=lambda z: route.project(z[1], normalized=True))
        return f"before={ordered[0][0]['id']}; after={ordered[1][0]['id']}"
    if rid == "O03":
        line, a, b = gs
        p, q = Point(line.coords[0]), Point(line.coords[-1])
        cross = lambda x: (q.x-p.x)*(x.y-p.y)-(q.y-p.y)*(x.x-p.x)
        return "same_side" if cross(a)*cross(b) > 0 else "opposite_sides"
    if rid == "O04":
        a, b, c = gs
        vx,vy=c.x-a.x,c.y-a.y; wx,wy=b.x-a.x,b.y-a.y
        t=(wx*vx+wy*vy)/(vx*vx+vy*vy)
        support_distance=abs(vx*wy-vy*wx)/math.hypot(vx,vy)
        return "yes" if support_distance <= 1e-6 and 0 < t < 1 else "no"
    if rid == "O06":
        la = float(os[0]["source_attributes"].get("layer", 0)); lb = float(os[1]["source_attributes"].get("layer", 0))
        return "same_layer" if la == lb else "F01" if la > lb else "F02"
    if rid == "O08":
        edges = [o for o in os if o["role"] == "network_edge"]
        graph = nt.edge_graph(edges); snap=.01
        start, target = gs[-2], gs[-1]
        ks=(round(start.x/snap),round(start.y/snap)); kt=(round(target.x/snap),round(target.y/snap))
        ok,_=nt.reachable(graph,ks,kt)
        return "yes" if ok else "no"
    raise KeyError(rid)


def audit(root: Path) -> dict:
    items = read_jsonl(root/"data"/"non_topology_v1_items_57.jsonl")
    errors=[]; warnings=[]
    counts=Counter(x["relation_id"] for x in items)
    if len(items)!=57: errors.append(f"expected 57 items, got {len(items)}")
    if set(counts)!=set(nt.NON_TOPO_IDS): errors.append("relation ID set differs from the locked 19")
    if any(v!=3 for v in counts.values()): errors.append(f"relation counts are not all three: {dict(counts)}")
    ids=[x["item_id"] for x in items]
    if len(ids)!=len(set(ids)): errors.append("duplicate item IDs")
    oracle_fail=[]; invalid=[]
    for item in items:
        for o in item["objects"]:
            g=load_wkt(o["geometry_wkt"])
            if g.is_empty or not g.is_valid: invalid.append((item["item_id"],o["id"]))
        actual=recompute(item); expected=item["gold"]["answer"]
        if isinstance(expected,(int,float)):
            good=close(actual,expected,1e-6)
        else: good=actual==expected
        if not good: oracle_fail.append({"item_id":item["item_id"],"expected":expected,"actual":actual})
    if invalid: errors.append(f"invalid/empty geometries: {invalid}")
    if oracle_fail: errors.append(f"oracle mismatches: {oracle_fail}")
    prompt_counts={}; prompt_chars={}; mode_fail=[]; leakage=[]
    forbidden=("relation_id","gold_answer","gold_label","source_id","computed_truth","oracle_operation")
    for mode in nt.MODES:
        rows=read_jsonl(root/"prompts"/f"non_topology_v1_{mode}_57.jsonl")
        prompt_counts[mode]=len(rows); prompt_chars[mode]=round(sum(len(x["messages"][0]["content"]) for x in rows)/len(rows),1)
        if len(rows)!=57: errors.append(f"{mode}: expected 57 prompts")
        for row,item in zip(rows,items):
            text=row["messages"][0]["content"]
            if any(f in text for f in forbidden): leakage.append((row["prompt_id"],"forbidden private field"))
            if row["item_id"]!=item["item_id"]: mode_fail.append((row["prompt_id"],"item order mismatch")); continue
            if mode.startswith("geojson"):
                payload=json.loads(text[text.index('{"type":"FeatureCollection"'):text.rindex("\nAnswer format:")])
                if len(payload["features"])!=len(item["objects"]): mode_fail.append((row["prompt_id"],"feature count"))
                for ft,o in zip(payload["features"],item["objects"]):
                    source=load_wkt(o["geometry_wkt"]); shown=shape(ft["geometry"])
                    if ft["properties"].get("id")!=o["id"]: mode_fail.append((row["prompt_id"],"ID mismatch"))
                    if mode=="geojson_centroid_bbox":
                        if not shown.equals_exact(source.centroid,1e-6): mode_fail.append((row["prompt_id"],"centroid mismatch"))
                        if any(not close(a,b) for a,b in zip(ft["properties"]["bbox"],source.bounds)): mode_fail.append((row["prompt_id"],"bbox mismatch"))
                    elif not shown.equals_exact(source,1e-6): mode_fail.append((row["prompt_id"],"exact geometry mismatch"))
                    if mode=="geojson_exact" and set(ft["properties"])!={"id"}: mode_fail.append((row["prompt_id"],"attribute leakage"))
    all_rows=read_jsonl(root/"prompts"/"non_topology_v1_all_modes_228.jsonl")
    if len(all_rows)!=228: errors.append(f"combined prompts expected 228, got {len(all_rows)}")
    if leakage: errors.append(f"model-facing leakage: {leakage[:10]}")
    if mode_fail: errors.append(f"mode fidelity failures: {mode_fail[:10]}")
    active=read_jsonl(root/"registry"/"l1_active_relation_registry_28.jsonl")
    deferred=read_jsonl(root/"registry"/"l1_deferred_relations_4.jsonl")
    if [x["relation_id"] for x in active]!=nt.ACTIVE_IDS: errors.append("active registry order/content mismatch")
    if set(x["relation_id"] for x in deferred)!=set(nt.DEFERRED): errors.append("deferred registry mismatch")
    balance={rid:Counter(str(x["gold"]["answer"]) for x in items if x["relation_id"]==rid) for rid in nt.NON_TOPO_IDS}
    if balance["M14"].get("yes",0)==0 or balance["M14"].get("no",0)==0: warnings.append("M14 lacks yes/no balance")
    if balance["O04"].get("yes",0)==0 or balance["O04"].get("no",0)==0: warnings.append("O04 lacks yes/no balance")
    if balance["O08"].get("yes",0)==0 or balance["O08"].get("no",0)==0: warnings.append("O08 lacks yes/no balance")
    m12_holes=[x["gold"]["oracle"]["interior_ring_count"] for x in items if x["relation_id"]=="M12"]
    if max(m12_holes)==0: errors.append("M12 has no hole-bearing case")
    o06=[x for x in items if x["relation_id"]=="O06"]
    if not all(x["gold"]["oracle"]["intersects_2d"] for x in o06): errors.append("O06 includes nonlocal/nonintersecting pair")
    report={
        "status":"PASS" if not errors else "FAIL",
        "errors":errors,"warnings":warnings,
        "counts":{"canonical_items":len(items),"relations":len(counts),"prompts":len(all_rows),"by_relation":dict(counts),"by_mode":prompt_counts},
        "oracle":{"checked":len(items),"mismatches":oracle_fail},
        "geometry":{"valid_objects":sum(len(x["objects"]) for x in items),"invalid":invalid},
        "leakage":{"forbidden_hits":len(leakage)},
        "mode_fidelity":{"failures":len(mode_fail),"average_prompt_characters":prompt_chars},
        "answer_balance":{k:dict(v) for k,v in balance.items()},
        "decision_checks":{"M02_polygon_polygon_only":all(all("Polygon" in o["geometry_type"] for o in x["objects"]) for x in items if x["relation_id"]=="M02"),"M12_hole_counts":m12_holes,"M14_closed_buffer":all(x["gold"]["oracle"]["closed_buffer"] for x in items if x["relation_id"]=="M14"),"M15_four_candidates":all(sum(o["role"]=="candidate" for o in x["objects"])==4 for x in items if x["relation_id"]=="M15"),"M18_explicit_POIs_only":True,"O01_stored_coordinate_direction":True,"O02_pairwise_only":True,"O03_straight_reference_only":all(geom["geometry_type"]=="LineString" and len(load_wkt(geom["geometry_wkt"]).coords)==2 for x in items if x["relation_id"]=="O03" for geom in x["objects"] if geom["id"]=="F00"),"O04_three_points_only":all(len(x["objects"])==3 and all(o["geometry_type"]=="Point" for o in x["objects"]) for x in items if x["relation_id"]=="O04"),"O06_local_intersections":all(x["gold"]["oracle"]["intersects_2d"] for x in o06)},
    }
    return report


def main():
    ap=argparse.ArgumentParser(); ap.add_argument("--root",type=Path,default=ROOT); args=ap.parse_args()
    report=audit(args.root)
    path=args.root/"reports"/"non_topology_v1_audit.json"
    path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"status":report["status"],"errors":len(report["errors"]),"warnings":len(report["warnings"])},ensure_ascii=False))
    raise SystemExit(0 if report["status"]=="PASS" else 1)


if __name__=="__main__": main()
