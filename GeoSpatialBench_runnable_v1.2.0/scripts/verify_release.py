#!/usr/bin/env python3
"""Verify release hashes, city scope, prompt leakage and small-file packaging."""
import argparse
from collections import Counter
import json
from pathlib import Path
import re

from io_utils import ROOT, read_rows, index_rows, sha256, write_json

EXCLUDED = {'outputs','.git','__pycache__','.venv','.runtime','local_data'}

def release_files():
    files = []
    for path in ROOT.rglob('*'):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if any(part in EXCLUDED for part in rel.parts):
            continue
        if rel.parts[0]=='examples' and 'images' in rel.parts:
            continue
        if rel.as_posix() in ('RELEASE_MANIFEST.json','SHA256SUMS.txt'):
            continue
        files.append(path)
    return sorted(files,key=lambda p:p.relative_to(ROOT).as_posix())

def refresh():
    files = [{'path':p.relative_to(ROOT).as_posix(),'bytes':p.stat().st_size,'sha256':sha256(p)} for p in release_files()]
    manifest = {'release':(ROOT/'VERSION').read_text(encoding='utf-8').strip(),'date':'2026-10-14','paper':'https://arxiv.org/abs/2610.12881',
                'example_cities':['stockholm'],'example_models':['Qwen3.5-9B'],
                'total_bytes':sum(p['bytes'] for p in files),'files':files}
    write_json(ROOT/'RELEASE_MANIFEST.json',manifest)
    (ROOT/'SHA256SUMS.txt').write_text(''.join(p['sha256']+'  '+p['path']+'\n' for p in files),encoding='utf-8')

def verify():
    manifest = json.loads((ROOT/'RELEASE_MANIFEST.json').read_text(encoding='utf-8'))
    expected = {r['path']:r for r in manifest['files']}
    actual = {p.relative_to(ROOT).as_posix():p for p in release_files()}
    if set(expected)!=set(actual):
        raise ValueError(f'File inventory mismatch: missing={sorted(set(expected)-set(actual))}, extra={sorted(set(actual)-set(expected))}')
    for name,path in actual.items():
        if sha256(path)!=expected[name]['sha256'] or path.stat().st_size!=expected[name]['bytes']:
            raise ValueError(f'Hash/size mismatch: {name}')
        if path.stat().st_size>8*1024*1024:
            raise ValueError(f'Unexpected large file: {name}')
        allowed_vector = name == 'examples/stockholm/vector/stockholm_sample.gpkg'
        if path.suffix.lower() in ('.gpkg','.shp','.tif','.pt','.safetensors','.zip') and not allowed_vector:
            raise ValueError(f'Raw data, weights or nested archive: {name}')
        if path.suffix.lower() not in ('.png','.jpg','.jpeg','.webp','.gpkg'):
            text = path.read_text(encoding='utf-8')
            if re.search(r'\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}',text):
                raise ValueError(f'Possible API credential: {name}')
            if path.name != 'verify_release.py' and re.search(r'[CD]:\\(?:Users|work|software)\\|/workspace/relation/',text):
                raise ValueError(f'Machine-specific path: {name}')
    checksums = ''.join(r['sha256']+'  '+r['path']+'\n' for r in manifest['files'])
    if (ROOT/'SHA256SUMS.txt').read_text(encoding='utf-8')!=checksums:
        raise ValueError('SHA256SUMS inventory differs from manifest')
    case = ROOT/'examples/stockholm'
    counts = {}
    for name,key in [('qa/items.jsonl','item_id'),('editing/tasks.jsonl','task_id')]:
        rows = read_rows(case/name)
        index_rows(rows,key)
        relation_counts = Counter(row['relation_id'] for row in rows)
        if len(rows)!=700 or len(relation_counts)!=28 or set(relation_counts.values())!={25}:
            raise ValueError(f'Invalid frozen city quotas: {name}')
        if any(not row[key].startswith('STO-') for row in rows):
            raise ValueError('Unexpected city in example data')
        counts[name] = len(rows)
    for form in ('open','mcq4'):
        if set(index_rows(read_rows(case/f'qa/gold_{form}.jsonl'),'item_id'))!=set(index_rows(read_rows(case/'qa/items.jsonl'),'item_id')):
            raise ValueError(f'Gold/item mismatch: {form}')
    for path in (case/'prompts').glob('*.jsonl'):
        rows = read_rows(path)
        if len(rows)!=700:
            raise ValueError(f'Incomplete shipped prompt set: {path.name}')
        index_rows(rows,'prompt_id')
        for row in rows:
            if any(key in row for key in ('gold','expected','ground_truth_wkt','provenance')):
                raise ValueError(f'Reference fields in prompt row: {path.name}')
            text = json.dumps(row['messages'])
            if re.search(r'ground_truth|gold_answer|predicted_F0_layer|source_object_id|gis_osm_|OSM_Geofabrik:|"oracle"',text):
                raise ValueError(f'Reference/provenance leakage in messages: {path.name}')
    other_cities = [p.name for p in (ROOT/'examples').iterdir() if p.is_dir() and p.name!='stockholm']
    if other_cities:
        raise ValueError(f'Other cities unexpectedly packaged: {other_cities}')
    vector_file = case/'vector/stockholm_sample.gpkg'
    vector_manifest = json.loads((case/'vector/data_manifest.json').read_text(encoding='utf-8'))
    if sha256(vector_file)!=vector_manifest['sha256']:
        raise ValueError('Vector example differs from its data manifest')
    import pyogrio
    actual_layers = {str(name) for name,_ in pyogrio.list_layers(vector_file)}
    if actual_layers != {'pois','roads','buildings','landuse'}:
        raise ValueError(f'Unexpected vector layer set: {actual_layers}')
    vector_counts = {}
    for role,record in vector_manifest['layers'].items():
        info = pyogrio.read_info(vector_file,layer=role)
        if info['crs'] != 'EPSG:3006' or info['features']!=record['features'] or 'source_id' not in info['fields']:
            raise ValueError(f'Vector layer count, CRS or source ID field mismatch: {role}')
        if role=='roads' and 'osm_layer' not in info['fields']:
            raise ValueError('Vector roads lack required stacking layer')
        vector_counts[role] = info['features']
    return {'status':'PASS','packaged_files':len(expected),'packaged_bytes':manifest['total_bytes'],
            'city':'stockholm','model':'Qwen3.5-9B','canonical_counts':counts,'source_vector_counts':vector_counts}

if __name__=='__main__':
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--refresh',action='store_true')
    args = ap.parse_args()
    if args.refresh:
        refresh()
    print(json.dumps(verify(),indent=2))
