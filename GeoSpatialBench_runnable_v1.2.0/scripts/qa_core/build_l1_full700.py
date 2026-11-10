#!/usr/bin/env python3
"""Internal v5 L1 construction core: 28 relations x 25 items.

No easy/medium/hard labels are assigned. Instead, objective design factors and
mode-specific information sufficiency are stored for later empirical analysis.

Run ``build_city_case.py`` for normal use. That wrapper supplies the city,
layers, CRS, provenance and output paths from one JSON configuration file.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import random
from collections import Counter, defaultdict
from pathlib import Path

import geopandas as gpd
import shapely
from shapely import affinity
from shapely.geometry import LineString, Point, Polygon, box
from shapely.wkt import loads as load_wkt


ROOT=Path(__file__).resolve().parents[2]
GPKG=Path("CONFIG_REQUIRED_USE_build_city_case.py")
MODES=["wkt_exact","geojson_centroid_bbox","geojson_exact","geojson_exact_attributes"]
ACTIVE=["T01","T02","T03","T04","T05","T06","T08","T09","T11","D01","D02","D04","D05","D06","M01","M02","M04","M08","M12","M14","M15","M18","O01","O02","O03","O04","O06","O08"]
RNG=random.Random(20260819)


def import_script(name,path):
    spec=importlib.util.spec_from_file_location(name,path); mod=importlib.util.module_from_spec(spec); spec.loader.exec_module(mod); return mod


NT=import_script("nt700",Path(__file__).with_name("build_non_topology_v1.py"))
FORMS=import_script("forms700",Path(__file__).with_name("build_l1_two_question_forms_v1.py"))


def write_jsonl(path,rows):
    path.parent.mkdir(parents=True,exist_ok=True); path.write_text("".join(json.dumps(x,ensure_ascii=False,separators=(",",":"),default=json_default)+"\n" for x in rows),encoding="utf-8")


def write_json(path,value):
    path.parent.mkdir(parents=True,exist_ok=True); path.write_text(json.dumps(value,ensure_ascii=False,indent=2,default=json_default)+"\n",encoding="utf-8")


def json_default(value):
    if hasattr(value,"item"):
        return value.item()
    if hasattr(value,"isoformat"):
        return value.isoformat()
    if isinstance(value,Path):
        return str(value)
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def largest_polygon(g):
    if g.geom_type=="MultiPolygon": return max(g.geoms,key=lambda x:x.area)
    return g


def line_part(g):
    if g.geom_type=="MultiLineString": return max(g.geoms,key=lambda x:x.length)
    return g


def record_source(row,layer):
    attrs={}
    for k in row.index:
        if k=="geometry": continue
        value=row.get(k)
        if value is None: continue
        if hasattr(value,"item"):
            value=value.item()
        if isinstance(value,float) and math.isnan(value):
            continue
        if hasattr(value,"isoformat"):
            value=value.isoformat()
        attrs[k]=value
    return {"source_id":f"{layer}:{row.get('osm_id','unknown')}","source_layer":layer,"attributes":attrs}


def item(rid,idx,objects,task,answer_format,answer,oracle,scoring,construction="observed",factors=None,suff=None):
    x=NT.make_item(rid,idx,objects,task,answer_format,answer,oracle,scoring,construction,suff)
    x["item_id"]=f"STO-L1F-{rid}-{idx:03d}"; x["version"]="v5.l1.full700.1"; x["design_factors"]=factors or {}; return x


def load_data(gpkg):
    bbox=(17.95,59.29,18.12,59.37)
    def read(layer): return gpd.read_file(gpkg,layer=layer,bbox=bbox).to_crs(3006)
    pois=read("gis_osm_pois_free").dropna(subset=["geometry"]).reset_index(drop=True)
    roads=read("gis_osm_roads_free").dropna(subset=["geometry"]).reset_index(drop=True)
    buildings=read("gis_osm_buildings_a_free").dropna(subset=["geometry"]).reset_index(drop=True)
    landuse=read("gis_osm_landuse_a_free").dropna(subset=["geometry"]).reset_index(drop=True)
    roads["_length"]=roads.length; buildings["_area"]=buildings.area; landuse["_area"]=landuse.area
    roads=roads[(roads._length>30)&(roads._length<1200)].sample(frac=1,random_state=19).reset_index(drop=True)
    buildings=buildings[(buildings._area>50)&(buildings._area<30000)].sample(frac=1,random_state=20).reset_index(drop=True)
    landuse=landuse[(landuse._area>20000)&(landuse._area<3000000)].sample(frac=1,random_state=21).reset_index(drop=True)
    pois=pois.sample(frac=1,random_state=18).reset_index(drop=True)
    return pois,roads,buildings,landuse


def pobj(row,fid,role="object",geom_override=None,derivation=None):
    src=record_source(row,"gis_osm_pois_free"); return NT.obj(fid,role,geom_override or row.geometry,src,derivation)
def robj(row,fid,role="road",geom_override=None,derivation=None):
    src=record_source(row,"gis_osm_roads_free"); return NT.obj(fid,role,geom_override or line_part(row.geometry),src,derivation)
def bobj(row,fid,role="polygon",geom_override=None,derivation=None):
    src=record_source(row,"gis_osm_buildings_a_free"); return NT.obj(fid,role,geom_override or largest_polygon(row.geometry),src,derivation)
def lobj(row,fid,role="region",geom_override=None,derivation=None):
    src=record_source(row,"gis_osm_landuse_a_free"); return NT.obj(fid,role,geom_override or largest_polygon(row.geometry),src,derivation)


def safe_poly(row): return largest_polygon(row.geometry).buffer(0)
def safe_line(row): return line_part(row.geometry).simplify(.2)


TOPO_PRIORITY=["equals","contains","within","covers","overlaps","crosses","touches","intersects","disjoint"]


def topo_standard(rid,idx,a,b,answer,srca,srcb,construction,factors):
    os=[NT.obj("F01","object",a,srca),NT.obj("F02","object",b,srcb)]
    task="What is the most specific topological relationship from F01 to F02? Use the priority equals > contains > within > covers > overlaps > crosses > touches > intersects > disjoint."
    return item(rid,idx,os,task,"one lowercase relationship label",answer,{"predicate":answer,"priority":TOPO_PRIORITY},{"type":"exact"},construction,factors,{"wkt_exact":True,"geojson_centroid_bbox":False,"geojson_exact":True,"geojson_exact_attributes":True})


def topology_items(pois,roads,buildings):
    out=[]
    # T01 equals: point, reversed/densified line, reordered polygon ring.
    for i in range(25):
        kind=i%3
        if kind==0:
            r=pois.iloc[i]; a=r.geometry; b=Point(a.x,a.y); sa=sb=record_source(r,"gis_osm_pois_free")
        elif kind==1:
            r=roads.iloc[i]; a=safe_line(r); b=LineString(list(a.coords)[::-1]); sa=sb=record_source(r,"gis_osm_roads_free")
        else:
            r=buildings.iloc[i]; a=safe_poly(r); coords=list(a.exterior.coords); k=1+(i%max(1,len(coords)-2)); b=Polygon(coords[k:-1]+coords[:k]+[coords[k]]); sa=sb=record_source(r,"gis_osm_buildings_a_free")
        out.append(topo_standard("T01",i+1,a,b,"equals",sa,sb,"observed geometry with representation-preserving reorder",{"geometry_signature":f"{a.geom_type}-{b.geom_type}","reordered":kind>0}))
    # T02 disjoint: real point/polygon, line/polygon, polygon/polygon pairs selected far enough apart.
    for i in range(25):
        p=safe_poly(buildings.iloc[i]); sp=record_source(buildings.iloc[i],"gis_osm_buildings_a_free")
        kind=i%3
        if kind==0:
            candidates=pois[pois.geometry.distance(p)>2]; r=candidates.iloc[i%len(candidates)]; a,b=r.geometry,p; sa=record_source(r,"gis_osm_pois_free"); sb=sp
        elif kind==1:
            candidates=roads[roads.geometry.distance(p)>2]; r=candidates.iloc[i%len(candidates)]; a,b=safe_line(r),p; sa=record_source(r,"gis_osm_roads_free"); sb=sp
        else:
            q=safe_poly(buildings.iloc[(i+200)%len(buildings)]); a,b=p,q; sa=sp; sb=record_source(buildings.iloc[(i+200)%len(buildings)],"gis_osm_buildings_a_free")
            if not a.disjoint(b): b=affinity.translate(b,xoff=2000+i*5)
        assert a.disjoint(b)
        out.append(topo_standard("T02",i+1,a,b,"disjoint",sa,sb,"observed pair; transparent translation only if required",{"geometry_signature":f"{a.geom_type}-{b.geom_type}","minimum_distance_m":a.distance(b)}))
    # T04 touches: point-line endpoint, two lines sharing endpoint, adjacent polygons sharing an edge.
    for i in range(25):
        r=roads.iloc[50+i]; g=safe_line(r); s=record_source(r,"gis_osm_roads_free"); kind=i%3
        if kind==0: a,b=Point(g.coords[0]),g; sa=sb=s
        elif kind==1:
            p0,p1=Point(g.coords[-2]),Point(g.coords[-1]); dx,dy=p1.x-p0.x,p1.y-p0.y; n=max(math.hypot(dx,dy),1e-9); anchor=p1
            a=LineString([p0,p1]); b=LineString([anchor,(anchor.x-10*dy/n,anchor.y+10*dx/n)]); sa=sb=s
        else:
            c=g.centroid; w=20+i%5; a=box(c.x-w,c.y-w,c.x,c.y+w); b=box(c.x,c.y-w,c.x+w,c.y+w); sa=sb=s
        assert a.touches(b)
        out.append(topo_standard("T04",i+1,a,b,"touches",sa,sb,"transparent construction anchored to real OSM road",{"geometry_signature":f"{a.geom_type}-{b.geom_type}"}))
    # T05 crosses: line-line and line-polygon.
    for i in range(25):
        r=roads.iloc[100+i]; g=safe_line(r); c=g.interpolate(.5,normalized=True); s=record_source(r,"gis_osm_roads_free")
        if i%2==0:
            a=LineString([(c.x-40,c.y),(c.x+40,c.y)]); b=LineString([(c.x,c.y-40),(c.x,c.y+40)])
        else:
            a=box(c.x-25,c.y-25,c.x+25,c.y+25); b=LineString([(c.x-50,c.y),(c.x+50,c.y)])
        assert a.crosses(b)
        out.append(topo_standard("T05",i+1,a,b,"crosses",s,s,"transparent construction anchored to real OSM road",{"geometry_signature":f"{a.geom_type}-{b.geom_type}"}))
    # T06 contains/within inverse wording.
    for i in range(25):
        r=buildings.iloc[100+i]; p=safe_poly(r); s=record_source(r,"gis_osm_buildings_a_free"); q=p.representative_point(); d=max(1.0,q.distance(p.boundary)*.4)
        contained=Point(q.x,q.y) if i%3==0 else LineString([(q.x-d,q.y),(q.x+d,q.y)]) if i%3==1 else q.buffer(d*.7,resolution=4)
        if not p.contains(contained): contained=q.buffer(max(.1,q.distance(p.boundary)*.1),resolution=4)
        if i%2==0: a,b,ans=p,contained,"contains"
        else: a,b,ans=contained,p,"within"
        assert (a.contains(b) if ans=="contains" else a.within(b))
        out.append(topo_standard("T06",i+1,a,b,ans,s,s,"contained geometry transparently derived inside real building",{"geometry_signature":f"{a.geom_type}-{b.geom_type}","direction":ans}))
    # T08 overlaps: same-dimensional line or polygon partial overlap.
    for i in range(25):
        r=roads.iloc[150+i]; g=safe_line(r); c=g.interpolate(.5,normalized=True); s=record_source(r,"gis_osm_roads_free")
        if i%2==0:
            a=LineString([(c.x-60,c.y),(c.x+20,c.y)]); b=LineString([(c.x-20,c.y),(c.x+60,c.y)])
        else:
            a=box(c.x-40,c.y-30,c.x+10,c.y+30); b=box(c.x-10,c.y-30,c.x+40,c.y+30)
        assert a.overlaps(b)
        out.append(topo_standard("T08",i+1,a,b,"overlaps",s,s,"transparent construction anchored to real OSM road",{"geometry_signature":f"{a.geom_type}-{b.geom_type}"}))
    # T09 covers: boundary points and boundary line segments; contains is false.
    for i in range(25):
        r=buildings.iloc[200+i]; p=safe_poly(r); s=record_source(r,"gis_osm_buildings_a_free"); coords=list(p.exterior.coords)
        b=Point(coords[i%(len(coords)-1)]) if i%2==0 else LineString([coords[i%(len(coords)-1)],coords[(i+1)%(len(coords)-1)]])
        assert p.covers(b) and not p.contains(b)
        out.append(topo_standard("T09",i+1,p,b,"covers",s,s,"boundary object derived from real building",{"geometry_signature":f"{p.geom_type}-{b.geom_type}","boundary_case":True}))
    # T03 general intersects is an independent boolean query, balanced yes/no.
    inter_sources=[x for x in out if x["relation_id"] in {"T01","T02","T04","T05","T08","T09"}]
    positive_sources=[x for x in inter_sources if x["relation_id"]!="T02"]
    negative_sources=[x for x in inter_sources if x["relation_id"]=="T02"]
    for i in range(25):
        pool=positive_sources if i%2==0 else negative_sources
        base=pool[((i//2)*7)%len(pool)]; a=load_wkt(base["objects"][0]["geometry_wkt"]); b=load_wkt(base["objects"][1]["geometry_wkt"]); truth=a.intersects(b)
        os=[dict(base["objects"][0],id="F01"),dict(base["objects"][1],id="F02")]
        out.append(item("T03",i+1,os,"Do F01 and F02 intersect in the standard GIS sense?","yes or no","yes" if truth else "no",{"intersects":truth,"specific_predicates":[p for p in ("equals","contains","within","covers","overlaps","crosses","touches") if getattr(a,p)(b)]},{"type":"exact"},"reused distinct real-anchored topology construction",{"geometry_signature":f"{a.geom_type}-{b.geom_type}"},{"wkt_exact":True,"geojson_centroid_bbox":False,"geojson_exact":True,"geojson_exact_attributes":True}))
    # T11 inherited strict contains = contains and not equals, balanced positives/negatives.
    for i in range(25):
        r=buildings.iloc[250+i]; p=safe_poly(r); s=record_source(r,"gis_osm_buildings_a_free"); q=p.representative_point(); d=max(.2,q.distance(p.boundary)*.2)
        if i<13:
            b=q if i%3==0 else LineString([(q.x-d,q.y),(q.x+d,q.y)]) if i%3==1 else q.buffer(d*.5,resolution=4)
        else:
            b=p if i%3==1 else Point(list(p.exterior.coords)[0]) if i%3==2 else affinity.translate(q,xoff=3000)
        truth=p.contains(b) and not p.equals(b)
        os=[NT.obj("F01","container",p,s),NT.obj("F02","candidate",b,s,derivation="transparent positive/negative strict-contains probe")]
        out.append(item("T11",i+1,os,"Under the inherited rule strict_contains(A,B) = contains(A,B) and not equals(A,B), does F01 strictly contain F02?","yes or no","yes" if truth else "no",{"definition":"contains(A,B) and not equals(A,B)","contains":p.contains(b),"equals":p.equals(b),"strict_contains":truth},{"type":"exact"},"probe derived from real building",{"geometry_signature":f"{p.geom_type}-{b.geom_type}"},{"wkt_exact":True,"geojson_centroid_bbox":False,"geojson_exact":True,"geojson_exact_attributes":True}))
    return out


def choose_point_pairs(pois,n,seed_offset=0,min_d=30,max_d=8000):
    pairs=[]; used=set(); rng=random.Random(1000+seed_offset)
    while len(pairs)<n:
        i=rng.randrange(len(pois)); a=pois.iloc[i]; dists=pois.geometry.distance(a.geometry); candidates=dists[(dists>min_d)&(dists<max_d)].index.tolist()
        if not candidates: continue
        j=rng.choice(candidates)
        if (i,j) in used: continue
        used.add((i,j)); pairs.append((a,pois.iloc[j]))
    return pairs


def choose_geometry_pairs(frame_a,frame_b,n,seed,min_d=10,max_d=3000,use_centroids=False):
    """Choose deterministic real-object pairs within a controlled distance band."""
    pairs=[]; used=set(); rng=random.Random(seed); attempts=0
    targets=frame_b.geometry.centroid if use_centroids else frame_b.geometry
    while len(pairs)<n and attempts<10000:
        attempts+=1; i=rng.randrange(len(frame_a)); a=frame_a.iloc[i]
        anchor=a.geometry.centroid if use_centroids else a.geometry
        dists=targets.distance(anchor); candidates=dists[(dists>min_d)&(dists<max_d)].index.tolist()
        if frame_a is frame_b: candidates=[j for j in candidates if j!=i]
        if not candidates: continue
        j=rng.choice(candidates); key=(i,int(j))
        if key in used: continue
        used.add(key); pairs.append((a,frame_b.loc[j]))
    if len(pairs)<n: raise RuntimeError(f"Only found {len(pairs)}/{n} geometry pairs for seed={seed}")
    return pairs


def typed_geometry(kind,row):
    if kind=="P": return row.geometry
    if kind=="L": return safe_line(row)
    if kind=="A": return safe_poly(row)
    raise KeyError(kind)


def typed_object(kind,row,fid,role="object",geom_override=None):
    if kind=="P": return pobj(row,fid,role,geom_override)
    if kind=="L": return robj(row,fid,role,geom_override)
    if kind=="A": return bobj(row,fid,role,geom_override)
    raise KeyError(kind)


def quota_pairs(pois,roads,buildings,seed_base,min_d=10,max_d=3000):
    """Return 25 pairs with PP=5 and PL/PA/LL/LA/AA=4 each."""
    frames={"P":pois,"L":roads,"A":buildings}; quotas={"PP":5,"PL":4,"PA":4,"LL":4,"LA":4,"AA":4}; result=[]
    for offset,(signature,count) in enumerate(quotas.items()):
        ka,kb=signature
        pairs=choose_geometry_pairs(frames[ka],frames[kb],count,seed_base+offset,min_d=min_d,max_d=max_d)
        result.extend((ka,a,kb,b) for a,b in pairs)
    return result


def direction_quota_cases(pois,buildings,rid,seed):
    """Jointly satisfy geometry-signature and direction-class quotas."""
    frames={"P":pois,"A":buildings}; signatures=["PP"]*9+["PA"]*4+["AP"]*4+["AA"]*8
    if rid=="D01": labels=["N"]*7+["E"]*6+["S"]*6+["W"]*6; classify=NT.sector4
    else: labels=["N"]*4+[x for x in ("NE","E","SE","S","SW","W","NW") for _ in range(3)]; classify=NT.sector8
    rng=random.Random(seed); rng.shuffle(labels); used=set(); cases=[]
    for signature,target in zip(signatures,labels):
        ka,kb=signature; found=None
        for _ in range(20000):
            ia=rng.randrange(len(frames[ka])); ib=rng.randrange(len(frames[kb]))
            if ka==kb and frames[ka] is frames[kb] and ia==ib: continue
            key=(ka,ia,kb,ib)
            if key in used: continue
            a,b=frames[ka].iloc[ia],frames[kb].iloc[ib]; ga,gb=typed_geometry(ka,a),typed_geometry(kb,b); ca,cb=ga.centroid,gb.centroid
            distance=ca.distance(cb)
            if not 30<distance<8000 or classify(NT.bearing(ca,cb))!=target: continue
            found=(ka,a,kb,b,target); used.add(key); break
        if found is None: raise RuntimeError(f"Could not build {rid} {signature} target={target}")
        cases.append(found)
    return cases


def non_top_items(pois,roads,buildings,landuse):
    out=[]
    # D01/D02/D04: each relation has 9 PP, 4 PA, 4 AP and 8 AA cases.
    for relation_index,rid in enumerate(("D01","D02","D04")):
        cases=direction_quota_cases(pois,buildings,rid,1200+relation_index)
        for i,(ka,a,kb,b,target_class) in enumerate(cases,1):
            ga,gb=typed_geometry(ka,a),typed_geometry(kb,b); ca,cb=ga.centroid,gb.centroid
            ang=NT.bearing(ca,cb); os=[typed_object(ka,a,"F01","reference",ga),typed_object(kb,b,"F02","target",gb)]
            if rid=="D01": task="Which four-direction class describes F02 relative to F01? Use N, E, S, or W; sector centres are 0°, 90°, 180°, and 270°."; ans=NT.sector4(ang); fmt="one token: N | E | S | W"; oracle={"bearing_deg":ang}; scoring={"type":"exact"}
            elif rid=="D02": task="Which eight-direction class describes F02 relative to F01? Use N, NE, E, SE, S, SW, W, or NW."; ans=NT.sector8(ang); fmt="one direction token"; oracle={"bearing_deg":ang}; scoring={"type":"exact"}
            else: task="What is the bearing from F01 to F02, measured clockwise from north?"; ans=round(ang,2); fmt="a number in degrees in [0,360)"; oracle={"bearing_deg":ang}; scoring={"type":"circular_numeric","absolute_tolerance_deg":1.0}
            if ka!="P" or kb!="P": task=task.replace("F02 relative to F01", "the centroid of F02 relative to the centroid of F01").replace("bearing from F01 to F02", "bearing from the centroid of F01 to the centroid of F02")
            oracle.update({"reference_point":"geometry centroid","F01_centroid":[ca.x,ca.y],"F02_centroid":[cb.x,cb.y]})
            boundary_margin=min(abs((ang-x+180)%360-180) for x in ([45,135,225,315] if rid=="D01" else [22.5+45*k for k in range(8)])) if rid!="D04" else None
            out.append(item(rid,i,os,task,fmt,ans,oracle,scoring,factors={"geometry_signature":f"{ga.geom_type}-{gb.geom_type}","direction_class":target_class,"angular_boundary_margin_deg":boundary_margin,"centroid_distance_m":ca.distance(cb)},suff={m:True for m in MODES}))
    # D05 actual road + nearby POI; D06 road axes.
    used_roads=[]
    for i in range(25):
        r=roads.iloc[300+i]; g=safe_line(r); start,end=Point(g.coords[0]),Point(g.coords[-1]); dists=pois.geometry.distance(start); pr=pois.loc[dists.nsmallest(20).index[(i%19)+1]]
        h=NT.bearing(start,end); tb=NT.bearing(start,pr.geometry); rel=((tb-h+180)%360)-180
        ans="front" if abs(rel)<=45 else "right" if 45<rel<=135 else "left" if -135<=rel<-45 else "back"
        os=[robj(r,"F01","directed_road",g),pobj(pr,"F02","target")]
        out.append(item("D05",i+1,os,"Stand at the first stored coordinate of road F01 and face toward its last stored coordinate. Is F02 in front, right, back, or left?","one token: front | right | back | left",ans,{"road_heading_deg":h,"target_bearing_deg":tb,"relative_angle_deg":rel},{"type":"exact"},factors={"geometry_signature":"LineString-Point","relative_boundary_margin_deg":min(abs(abs(rel)-45),abs(abs(rel)-135))},suff={"wkt_exact":True,"geojson_centroid_bbox":False,"geojson_exact":True,"geojson_exact_attributes":True}))
        used_roads.append(r)
    for i in range(25):
        r=roads.iloc[350+i]; g=safe_line(r); ang=NT.bearing(Point(g.coords[0]),Point(g.coords[-1])); ans=NT.axis4(ang)
        out.append(item("D06",i+1,[robj(r,"F01","road",g)],"What is the undirected principal orientation of F01, based on its first-to-last coordinate chord?","one token: N-S | NE-SW | E-W | NW-SE",ans,{"directed_bearing_deg":ang,"orientation_axis":ans},{"type":"exact"},factors={"geometry_signature":"LineString","vertex_count":len(g.coords)},suff={"wkt_exact":True,"geojson_centroid_bbox":False,"geojson_exact":True,"geojson_exact_attributes":True}))
    # M01 covers all six pair signatures; M04 remains point-only by definition.
    for i,(ka,a,kb,b) in enumerate(quota_pairs(pois,roads,buildings,2100),1):
        ga,gb=typed_geometry(ka,a),typed_geometry(kb,b); val=ga.distance(gb)
        os=[typed_object(ka,a,"F01",geom_override=ga),typed_object(kb,b,"F02",geom_override=gb)]
        out.append(item("M01",i,os,"What is the minimum planar Euclidean distance between geometry F01 and geometry F02?","a number in metres",round(val,2),{"distance_m":val,"operation":"minimum geometry distance"},{"type":"numeric","absolute_tolerance_m":1.0},factors={"geometry_signature":f"{ga.geom_type}-{gb.geom_type}","magnitude_m":val},suff={m:(m!="geojson_centroid_bbox" or ka==kb=="P") for m in MODES}))
    mpairs=choose_point_pairs(pois,25,2)
    for i,(a,b) in enumerate(mpairs,1):
        val=abs(a.geometry.x-b.geometry.x)+abs(a.geometry.y-b.geometry.y)
        out.append(item("M04",i,[pobj(a,"F01"),pobj(b,"F02")],"What is the Manhattan distance |Δx|+|Δy| between point F01 and point F02?","a number in metres",round(val,2),{"manhattan_distance_m":val},{"type":"numeric","absolute_tolerance_m":1.0},factors={"geometry_signature":"Point-Point","magnitude_m":val},suff={m:True for m in MODES}))
    # M02 nearest building pairs.
    for i in range(25):
        a=buildings.iloc[400+i]; pa=safe_poly(a); d=buildings.geometry.distance(pa); idx=d[(d>1)&(d<500)].nsmallest(1).index[0]; b=buildings.loc[idx]; pb=safe_poly(b); val=pa.boundary.distance(pb.boundary)
        out.append(item("M02",i+1,[bobj(a,"F01",geom_override=pa),bobj(b,"F02",geom_override=pb)],"What is the shortest boundary-to-boundary distance between polygon F01 and polygon F02?","a number in metres",round(val,2),{"boundary_distance_m":val},{"type":"numeric","absolute_tolerance_m":1.0},factors={"geometry_signature":"Polygon-Polygon","boundary_distance_m":val,"vertices":len(pa.exterior.coords)+len(pb.exterior.coords)}))
    # M08 nearby road pairs.
    for i in range(25):
        a=roads.iloc[450+i]; ga=safe_line(a); d=roads.geometry.distance(ga); idx=d[(d>1)&(d<800)].nsmallest(1).index[0]; b=roads.loc[idx]; gb=safe_line(b); val=float(shapely.hausdorff_distance(ga,gb,densify=.25))
        out.append(item("M08",i+1,[robj(a,"F01",geom_override=ga),robj(b,"F02",geom_override=gb)],"What is the symmetric densified discrete Hausdorff distance between F01 and F02? Use densify=0.25.","a number in metres",round(val,2),{"hausdorff_distance_m":val,"densify":.25,"symmetric":True},{"type":"numeric","absolute_tolerance_m":1.0},factors={"geometry_signature":"LineString-LineString","vertex_count":len(ga.coords)+len(gb.coords)}))
    # M12 polygons; five transparent hole probes.
    for i in range(25):
        r=buildings.iloc[500+i]; p=safe_poly(r); deriv=None; construction="observed"
        if i>=20:
            inner=affinity.scale(p,xfact=.3,yfact=.3,origin="centroid")
            if p.contains(inner): p=p.difference(inner); deriv="scaled interior ring added to real building"; construction="transparent construction"
        val=p.boundary.length; holes=len(p.interiors) if p.geom_type=="Polygon" else sum(len(g.interiors) for g in p.geoms)
        out.append(item("M12",i+1,[bobj(r,"F01",geom_override=p,derivation=deriv)],"What is the total boundary length of polygon F01? Include every exterior ring and every interior-hole ring.","a number in metres",round(val,2),{"total_boundary_length_m":val,"interior_ring_count":holes},{"type":"numeric","absolute_tolerance_m":1.0},construction,factors={"geometry_signature":p.geom_type,"interior_ring_count":holes,"vertex_count":len(p.exterior.coords) if p.geom_type=="Polygon" else None}))
    # M14 uses the same six-signature quota, with balanced threshold outcomes.
    for i,(ka,a,kb,b) in enumerate(quota_pairs(pois,roads,buildings,3100,min_d=10,max_d=500),1):
        ga,gb=typed_geometry(ka,a),typed_geometry(kb,b); oa=typed_object(ka,a,"F01",geom_override=ga); ob=typed_object(kb,b,"F02",geom_override=gb)
        dist=ga.distance(gb); radius=math.ceil(dist*1.2) if i%2 else max(1,math.floor(dist*.8)); ans=dist<=radius
        task=f"Does F02 intersect the closed {radius:g}-metre buffer of F01? (Equivalently, is the minimum geometry distance no greater than {radius:g} m?)"
        out.append(item("M14",i,[oa,ob],task,"yes or no","yes" if ans else "no",{"distance_m":dist,"buffer_radius_m":radius,"closed_buffer":True},{"type":"exact"},factors={"geometry_signature":f"{ga.geom_type}-{gb.geom_type}","threshold_margin_m":abs(dist-radius)},suff={m:(m!="geojson_centroid_bbox" or ka==kb=="P") for m in MODES}))
    # M15 four real POI candidates.
    for i in range(25):
        ref=pois.iloc[700+i]; d=pois.geometry.distance(ref.geometry); cand=pois.loc[d[(d>5)&(d<3000)].nsmallest(4).index].copy(); order=list(range(4)); random.Random(900+i).shuffle(order)
        os=[pobj(ref,"F00","reference")]; distances={}
        for j,k in enumerate(order,1):
            rec=cand.iloc[k]; os.append(pobj(rec,f"F{j:02d}","candidate")); distances[f"F{j:02d}"]=ref.geometry.distance(rec.geometry)
        nearest=min(distances,key=distances.get); vals=sorted(distances.values())
        out.append(item("M15",i+1,os,"Which one of the four candidates F01, F02, F03, and F04 has the smallest planar distance to reference F00?","one candidate ID",nearest,{"distance_by_candidate_m":distances,"nearest_margin_m":vals[1]-vals[0]},{"type":"exact"},factors={"candidate_count":4,"nearest_margin_m":vals[1]-vals[0]},suff={m:True for m in MODES}))
    # M18 real landuse regions + exactly ten supplied POIs.
    made=0
    for _,r in landuse.iterrows():
        if made>=25: break
        p=safe_poly(r); inside=pois[pois.geometry.map(p.covers)]
        if len(inside)<3: continue
        outside=pois[~pois.geometry.map(p.covers)].copy(); outside["_d"]=outside.geometry.distance(p)
        n_inside=min(3+(made%5),len(inside),9)
        chosen=list(inside.head(n_inside).iterrows())+list(outside.nsmallest(10-n_inside,"_d").iterrows())
        if len(chosen)!=10: continue
        os=[lobj(r,"F00",geom_override=p)]
        for j,(_,pr) in enumerate(chosen,1): os.append(pobj(pr,f"P{j:02d}","explicit_poi"))
        count=sum(p.covers(load_wkt(o["geometry_wkt"])) for o in os[1:]); area=p.area/1e6; density=count/area
        out.append(item("M18",made+1,os,"Using only the explicitly supplied POIs P01 through P10, what is their density covered by region F00? Count boundary POIs as inside, divide by F00 area in km², and round the answer to three decimal places.","a number in POIs/km² rounded to three decimal places",round(density,3),{"explicit_poi_count":10,"covered_poi_count":count,"area_km2":area,"density_per_km2":density},{"type":"numeric","absolute_tolerance":.001},factors={"explicit_poi_count":10,"covered_poi_count":count,"polygon_area_km2":area},suff={"wkt_exact":True,"geojson_centroid_bbox":False,"geojson_exact":True,"geojson_exact_attributes":True})); made+=1
    if made<25: raise RuntimeError(f"Only built {made} M18 cases")
    # O01/O02 using stored coordinate direction.
    for rid in ("O01","O02"):
        for i in range(25):
            r=roads.iloc[800+i]; g=safe_line(r); base=[.2,.5,.8] if rid=="O01" else [.25,.75]
            labels=[f"F{j+1:02d}" for j in range(len(base))]; random.Random(10000+i+(0 if rid=="O01" else 100)).shuffle(labels)
            os=[robj(r,"F00","route",g)]+[NT.obj(label,"position",g.interpolate(frac,normalized=True),record_source(r,"gis_osm_roads_free"),f"sampled at normalized measure {frac}") for label,frac in zip(labels,base)]
            if rid=="O01": ans=">".join(labels); task="Along route F00 in its stored coordinate direction, order F01, F02, and F03 from first to last."; fmt="three IDs joined by >"; scoring={"type":"ordered_ids"}
            else: ans=f"before={labels[0]}; after={labels[1]}"; task="Along route F00 in its stored coordinate direction, which point is before the other?"; fmt="before=<ID>; after=<ID>"; scoring={"type":"pairwise_before_after"}
            out.append(item(rid,i+1,os,task,fmt,ans,{"normalized_measure_by_id":dict(zip(labels,base))},scoring,"points transparently sampled on a real road",{"route_length_m":g.length,"position_count":len(base)},suff={"wkt_exact":True,"geojson_centroid_bbox":False,"geojson_exact":True,"geojson_exact_attributes":True}))
    # O03/O04 straight chords.
    for rid in ("O03","O04"):
        for i in range(25):
            r=roads.iloc[900+i]; chord,_=NT.straight_chord({"geometry_wkt":shapely.to_wkt(safe_line(r))}); src=record_source(r,"gis_osm_roads_free")
            if rid=="O03":
                sides=(1,1) if i%3==0 else (1,-1) if i%3==1 else (-1,-1); a=NT.offset_point(chord,.3,sides[0],20+i%10); b=NT.offset_point(chord,.7,sides[1],25+i%10); ans="same_side" if sides[0]==sides[1] else "opposite_sides"
                os=[NT.obj("F00","straight_reference_line",chord,src,"road endpoint chord"),NT.obj("F01","point",a,src,"offset probe"),NT.obj("F02","point",b,src,"offset probe")]; task="Relative to the directed straight line F00 (first coordinate to last), are F01 and F02 on the same side or on opposite sides?"; fmt="same_side or opposite_sides"; oracle={"cross_product_signs":list(sides)}
            else:
                # Integer-grid probes remain exactly collinear after the six-decimal
                # GeoJSON serialization used by the prompt emitter. The anchor is
                # near a real road midpoint, while the tested relation is transparent.
                mid=chord.interpolate(.5,normalized=True); ox,oy=round(mid.x),round(mid.y)
                vx0,vy0=chord.coords[-1][0]-chord.coords[0][0],chord.coords[-1][1]-chord.coords[0][1]; norm=math.hypot(vx0,vy0)
                dx,dy=int(round(vx0/norm*50)),int(round(vy0/norm*50))
                if dx==0 and dy==0: dx=50
                a=Point(ox,oy); c=Point(ox+2*dx,oy+2*dy); typ=i%3
                if typ==0: b=Point(ox+dx,oy+dy)
                elif typ==1: b=Point(ox+3*dx,oy+3*dy)
                else:
                    px=-1 if dy>0 else 1; py=1 if dx>=0 else -1; off=1+(i%4)
                    b=Point(ox+dx+px*off,oy+dy+py*off)
                vx,vy=c.x-a.x,c.y-a.y; wx,wy=b.x-a.x,b.y-a.y; proj=(wx*vx+wy*vy)/(vx*vx+vy*vy); sd=abs(vx*wy-vy*wx)/math.hypot(vx,vy); truth=sd<=1e-12 and 0<proj<1; ans="yes" if truth else "no"
                os=[NT.obj("F01","endpoint",a,src,"integer-grid probe near road midpoint"),NT.obj("F02","candidate_between",b,src,"integer-grid probe"),NT.obj("F03","endpoint",c,src,"integer-grid probe near road midpoint")]; task="In the ordinary three-point collinear sense, is F02 strictly between F01 and F03?"; fmt="yes or no"; oracle={"distance_to_supporting_line_m":sd,"projection_parameter_unclamped":proj,"strict_between":truth,"collinearity_tolerance_m":1e-12,"serialization_safe":True}
            out.append(item(rid,i+1,os,task,fmt,ans,oracle,{"type":"exact"},"transparent probes anchored to real road geometry",{"geometry_signature":"LineString-Point-Point" if rid=="O03" else "Point-Point-Point"},suff={m:True if rid=="O04" else m!="geojson_centroid_bbox" for m in MODES}))
    # O06 intersecting road pairs with layer values; balance F01/F02/tie.
    rr=roads.copy(); rr["layer"]=rr["layer"].fillna(0).astype(float); special=rr[(rr.layer!=0)|(rr.bridge=="T")|(rr.tunnel=="T")]; diff=[]; same=[]; seen=set()
    for idxr,r in special.iterrows():
        for j in rr.sindex.query(r.geometry,predicate="intersects"):
            if idxr==j or tuple(sorted((int(idxr),int(j)))) in seen: continue
            seen.add(tuple(sorted((int(idxr),int(j))))); o=rr.loc[j]; (diff if r.layer!=o.layer else same).append((r,o))
        if len(diff)>=18 and len(same)>=9: break
    chosen=diff[:17]+same[:8]
    for i,(a,b) in enumerate(chosen,1):
        if i%2==0 and a.layer!=b.layer: a,b=b,a
        la,lb=float(a.layer),float(b.layer); ans="same_layer" if la==lb else "F01" if la>lb else "F02"
        out.append(item("O06",i,[robj(a,"F01"),robj(b,"F02")],"At their 2D overlap/crossing, which road is vertically above according to the supplied OSM layer values, or are they at the same layer? Layer is ordinal stacking, not elevation in metres.","F01 | F02 | same_layer",ans,{"F01_layer":la,"F02_layer":lb,"intersects_2d":a.geometry.intersects(b.geometry)},{"type":"exact"},factors={"layer_difference":abs(la-lb),"tie":la==lb},suff={"wkt_exact":False,"geojson_centroid_bbox":False,"geojson_exact":False,"geojson_exact_attributes":True}))
    # O08 endpoint graphs, alternating reachable/unreachable, 2-6 step paths.
    for i in range(25):
        r=roads.iloc[1000+i]; src={**record_source(r,"gis_osm_roads_free"),"geometry_wkt":shapely.to_wkt(safe_line(r))}; n=2+(i%5); edges,_=NT.split_chain(src,n,"E")
        if i%2==0:
            start=Point(load_wkt(edges[0]["geometry_wkt"]).coords[0]); target=Point(load_wkt(edges[-1]["geometry_wkt"]).coords[-1]); desc="reachable chain derived from real road"
        else:
            r2=roads.iloc[1200+i]; src2={**record_source(r2,"gis_osm_roads_free"),"geometry_wkt":shapely.to_wkt(safe_line(r2))}; extra,_=NT.split_chain(src2,2,"D"); edges+=extra; start=Point(load_wkt(edges[0]["geometry_wkt"]).coords[0]); target=Point(load_wkt(extra[-1]["geometry_wkt"]).coords[-1]); desc="two disconnected real-road-derived components"
        graph=NT.edge_graph(edges); snap=.01; ks=(round(start.x/snap),round(start.y/snap)); kt=(round(target.x/snap),round(target.y/snap)); ok,steps=NT.reachable(graph,ks,kt); os=edges+[NT.obj("S","start_node",start,derivation="edge endpoint"),NT.obj("T","target_node",target,derivation="edge endpoint")]
        out.append(item("O08",i+1,os,"Using only shared edge endpoints as network nodes (interior geometric crossings do not create nodes), is target T reachable from start S?","yes or no","yes" if ok else "no",{"reachable":ok,"shortest_path_edges":steps,"node_snap_tolerance_m":snap,"edge_count":len(edges)},{"type":"exact"},desc,{"edge_count":len(edges),"reasoning_steps":steps,"disconnected_components":2 if not ok else 1},suff={"wkt_exact":True,"geojson_centroid_bbox":False,"geojson_exact":True,"geojson_exact_attributes":True}))
    return out


def topology_mcq(item,index):
    rid=item["relation_id"]; answer=item["gold"]["answer"]; oracle=item["gold"]["oracle"]
    if rid not in {"T03","T11"}:
        correct=answer; distractors=FORMS.TOPO_CONFUSIONS[correct]; options,letter=FORMS.place_options(correct,distractors,index%4,item["item_id"]); task=item["task"]; semantics="most-specific topology label"
    elif rid=="T03":
        specifics=oracle["specific_predicates"]
        pool=["disjoint","touches without another listed specific relation","crosses or overlaps","equals, contains, within, or covers"]
        correct="disjoint" if not oracle["intersects"] else pool[3] if any(x in specifics for x in ("equals","contains","within","covers")) else pool[2] if any(x in specifics for x in ("crosses","overlaps")) else pool[1]
        options,letter=FORMS.place_options(correct,[x for x in pool if x!=correct],index%4,item["item_id"]); task="Which mutually exclusive intersection category describes F01 and F02?"; semantics="general intersects partition"
    else:
        pool=["F01 equals F02","F01 strictly contains F02","F01 covers but does not contain F02","F01 does not contain F02"]
        correct=pool[0] if oracle["equals"] else pool[1] if oracle["strict_contains"] else pool[2] if oracle["contains"] is False and load_wkt(item["objects"][0]["geometry_wkt"]).covers(load_wkt(item["objects"][1]["geometry_wkt"])) else pool[3]
        options,letter=FORMS.place_options(correct,[x for x in pool if x!=correct],index%4,item["item_id"]); task="Which statement is true under strict_contains(A,B)=contains(A,B) and not equals(A,B)?"; semantics="inherited strict-contains partition"
    return task,options,letter,{"mcq_semantics":semantics,"open_answer":answer}


def emit(items,root):
    open_by={m:[] for m in MODES}; mcq_by={m:[] for m in MODES}; open_gold=[]; mcq_gold=[]
    for index,x in enumerate(items):
        if x["relation_id"].startswith("T"): task,opts,letter,sem=topology_mcq(x,index)
        else: task,opts,letter,sem=FORMS.non_topology_mcq(x,index)
        open_gold.append({"item_id":x["item_id"],"relation_id":x["relation_id"],"answer":x["gold"]["answer"],"scoring":x["gold"]["scoring"]})
        mcq_gold.append({"item_id":x["item_id"],"relation_id":x["relation_id"],"answer_letter":letter,"answer_option":opts[FORMS.LETTERS.index(letter)],"options":opts,**sem})
        for mode in MODES:
            base=NT.prompt_row(x,mode); open_content=base["messages"][0]["content"]
            open_by[mode].append(FORMS.row(x["item_id"],mode,"open",open_content))
            mcq_content=FORMS.non_top_mcq_content(base,task,opts)
            mcq_by[mode].append(FORMS.row(x["item_id"],mode,"mcq4",mcq_content))
    open_all=[];mcq_all=[]
    for mode in MODES:
        write_jsonl(root/"prompts"/f"l1_full700_open_{mode}_700.jsonl",open_by[mode]); write_jsonl(root/"prompts"/f"l1_full700_mcq4_{mode}_700.jsonl",mcq_by[mode]); open_all+=open_by[mode];mcq_all+=mcq_by[mode]
    write_jsonl(root/"prompts"/"l1_full700_open_all_modes_2800.jsonl",open_all);write_jsonl(root/"prompts"/"l1_full700_mcq4_all_modes_2800.jsonl",mcq_all);write_jsonl(root/"prompts"/"l1_full700_two_forms_all_5600.jsonl",open_all+mcq_all)
    write_jsonl(root/"preds"/"l1_full700_open_private_gold_700.jsonl",open_gold);write_jsonl(root/"preds"/"l1_full700_mcq4_private_gold_700.jsonl",mcq_gold)
    return {"correct_letter_counts":dict(Counter(x["answer_letter"] for x in mcq_gold)),"open_prompts":len(open_all),"mcq_prompts":len(mcq_all)}


def main():
    ap=argparse.ArgumentParser();ap.add_argument("--root",type=Path,default=ROOT);ap.add_argument("--gpkg",type=Path,default=GPKG);a=ap.parse_args()
    pois,roads,buildings,landuse=load_data(a.gpkg); items=topology_items(pois,roads,buildings)+non_top_items(pois,roads,buildings,landuse); items.sort(key=lambda x:(ACTIVE.index(x["relation_id"]),x["item_id"])); write_jsonl(a.root/"data"/"l1_full700_items_700.jsonl",items); report=emit(items,a.root); report.update({"canonical_items":len(items),"relations":dict(Counter(x["relation_id"] for x in items)),"version":"v5.l1.full700.1","difficulty_labels_assigned":False});write_json(a.root/"reports"/"l1_full700_build_report.json",report);print(json.dumps(report,ensure_ascii=False))


if __name__=="__main__":main()
