#!/usr/bin/env python3
"""Portable GeoSpatialBench entry point. Run --help for the small command set."""
import argparse
import json
from pathlib import Path
import sys

from io_utils import ROOT, read_rows, write_json

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    commands = ap.add_subparsers(dest='command',required=True)
    inspect = commands.add_parser('inspect',help='List vector layers, geometry types, CRS and fields')
    inspect.add_argument('path',type=Path)
    init = commands.add_parser('init-config',help='Infer a configuration for a common four-layer GeoPackage')
    init.add_argument('--data',required=True,type=Path)
    init.add_argument('--city',required=True)
    init.add_argument('--id-prefix',required=True)
    init.add_argument('--out',required=True,type=Path)
    init.add_argument('--target-crs',default=None)
    check = commands.add_parser('check',help='Validate city inputs before generation')
    check.add_argument('--config',required=True,type=Path)
    build = commands.add_parser('build',help='Build 700 QA scenes and an independent editing pool')
    build.add_argument('--config',required=True,type=Path)
    build.add_argument('--task',choices=['qa','editing','both'],default='both')
    build.add_argument('--editing-per-relation',type=int,default=25)
    for name in ('prompts','render'):
        parser = commands.add_parser(name,help='Emit model inputs' if name=='prompts' else 'Render maps offline')
        parser.add_argument('--case',type=Path,default=ROOT/'examples/stockholm')
        parser.add_argument('--task',choices=['qa','editing','both'],default='both')
        parser.add_argument('--per-relation',type=int,default=0,help='0 means all items; 1 gives a 28-relation preview')
        if name=='prompts':
            parser.add_argument('--out',type=Path)
            parser.add_argument('--mode',choices=['geojson_exact','map_annotated','hybrid_geojson_exact'],default='geojson_exact')
    qa = commands.add_parser('evaluate-qa',help='Score one QA form with full gold-set denominator')
    qa.add_argument('--items',required=True,type=Path)
    qa.add_argument('--gold',required=True,type=Path)
    qa.add_argument('--predictions',required=True,type=Path)
    qa.add_argument('--form',choices=['open','mcq4'],required=True)
    qa.add_argument('--mode',choices=['geojson_exact','map_annotated','hybrid_geojson_exact'],default='geojson_exact')
    qa.add_argument('--out',required=True,type=Path)
    qa.add_argument('--allow-partial',action='store_true')
    qa.add_argument('--scoring',choices=['current','legacy'],default='current')
    ed = commands.add_parser('evaluate-editing',help='Score structured edits or offline pixel edits')
    ed.add_argument('--tasks',required=True,type=Path)
    ed.add_argument('--predictions',required=True,type=Path)
    ed.add_argument('--out',required=True,type=Path)
    ed.add_argument('--allow-partial',action='store_true')
    ed.add_argument('--raster',action='store_true',help='Pixel geometry from this package renderer; raster metric is distinct from CSR')
    example = commands.add_parser('example',help='Recompute the bundled Stockholm model results offline')
    example.add_argument('--out',type=Path,default=ROOT/'outputs/stockholm_example')
    example.add_argument('--scoring',choices=['current','legacy'],default='current')
    quick = commands.add_parser('quickstart',help='Verify, score, build and prepare all modalities offline')
    quick.add_argument('--out',type=Path)
    quick.add_argument('--skip-build',action='store_true')
    quick.add_argument('--per-relation',type=int,default=1)
    run = commands.add_parser('run',help='Run and score a model on matched GeoJSON/Image/Together inputs')
    run.add_argument('--case',type=Path,default=ROOT/'examples/stockholm')
    run.add_argument('--out',type=Path,required=True)
    run.add_argument('--model',required=True)
    run.add_argument('--base-url',required=True)
    run.add_argument('--api-format',choices=['chat','responses'],default='chat')
    run.add_argument('--key-env',default='BENCHMARK_API_KEY')
    run.add_argument('--mode',choices=['all','geojson_exact','map_annotated','hybrid_geojson_exact'],default='geojson_exact')
    run.add_argument('--per-relation',type=int,default=1,help='1 gives 28 matched scenes per task; 0 uses all 700')
    run.add_argument('--max-tokens',type=int,default=4096)
    run.add_argument('--temperature',type=float,default=None)
    run.add_argument('--resume',action='store_true')
    run.add_argument('--dry-run',action='store_true')
    args = ap.parse_args(argv)
    if getattr(args,'per_relation',0)<0 or getattr(args,'editing_per_relation',25)<1:
        ap.error('Per-relation counts must be positive (or 0 to use all for prompts/render)')
    if args.command=='quickstart':
        from workflow import quickstart
        result = quickstart(args.out,args.per_relation,not args.skip_build)
    elif args.command=='run':
        from workflow import run_model
        result = run_model(args)
    elif args.command=='inspect':
        import geopandas as gpd
        import pyogrio
        result = []
        for layer,geometry_type in pyogrio.list_layers(args.path):
            info = pyogrio.read_info(args.path,layer=layer)
            result.append({'layer':layer,'geometry_type':geometry_type,'crs':info.get('crs'),
                           'features':info.get('features'),'fields':list(info['fields'])})
    elif args.command=='init-config':
        from configure import init_config
        result = init_config(args.data,args.city,args.id_prefix,args.out,args.target_crs)
    elif args.command=='check':
        from build_release import load_config
        config,crs,out,frames,diagnostics = load_config(args.config)
        result = {'status':'PASS','city':config['city_name'],'target_crs':crs.to_string(),
                  'data':diagnostics,'note':'Feature-count checks passed; full relation availability is checked during build'}
    elif args.command=='build':
        from build_release import build_city
        result = build_city(args.config,args.task,args.editing_per_relation)
    elif args.command=='render':
        from rendering import render_case
        result = render_case(args.case,args.task,args.per_relation)
    elif args.command=='prompts':
        from prompts import emit_qa,emit_editing
        out = args.out or args.case/'prompts'
        result = {}
        if args.task in ('qa','both'):
            result['qa'] = emit_qa(args.case,out,args.mode,args.per_relation)
        if args.task in ('editing','both'):
            result['editing'] = emit_editing(args.case,out,args.mode,args.per_relation)
    elif args.command=='evaluate-qa':
        from evaluation import evaluate_qa
        result = evaluate_qa(args.items,args.gold,args.predictions,args.form,args.out,args.mode,args.allow_partial,args.scoring)
    elif args.command=='evaluate-editing':
        from evaluation import evaluate_editing
        result = evaluate_editing(args.tasks,args.predictions,args.out,args.allow_partial,args.raster)
    else:
        from evaluation import evaluate_qa,evaluate_editing
        case = ROOT/'examples/stockholm'
        results = {}
        for form in ('open','mcq4'):
            results['qa_'+form] = evaluate_qa(case/'qa/items.jsonl',case/f'qa/gold_{form}.jsonl',
                                             case/f'predictions/qa_{form}.jsonl',form,args.out/f'qa_{form}.json',scoring=args.scoring)
        results['editing'] = evaluate_editing(case/'editing/tasks.jsonl',case/'predictions/editing.jsonl',args.out/'editing.json')
        result = {name:{k:r[k] for k in ('metric','correct','total','score','scoring_version')} for name,r in results.items()}
        write_json(args.out/'summary.json',result)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return 0

if __name__=='__main__':
    try:
        raise SystemExit(main())
    except (ValueError,RuntimeError,OSError) as exc:
        print(f'ERROR: {exc}',file=sys.stderr)
        raise SystemExit(1)
