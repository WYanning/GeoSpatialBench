"""Offline acceptance and matched-modality model runs for the portable release."""
from collections import Counter
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys

from io_utils import ROOT, read_rows, index_rows, write_json, write_rows, sha256
from prompts import choose, emit_qa, emit_editing
from evaluation import evaluate_qa, evaluate_editing

MODES = ('geojson_exact', 'map_annotated', 'hybrid_geojson_exact')

def stamp():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')

def fingerprint(case):
    return {name:sha256(Path(case)/name) for name in
            ('qa/items.jsonl','qa/gold_open.jsonl','qa/gold_mcq4.jsonl','editing/tasks.jsonl')}

def prepare_case(source, target, per_relation):
    """Persist actual evaluation subsets so preview scores use preview denominators."""
    source, target = Path(source).resolve(), Path(target).resolve()
    identity = {'source_sha256':fingerprint(source),'per_relation':per_relation}
    marker = target/'selection.json'
    if marker.exists():
        saved = json.loads(marker.read_text(encoding='utf-8'))
        if saved['identity'] != identity or saved['selected_sha256'] != fingerprint(target):
            raise ValueError('Input subset changed; use a new output directory')
        return saved
    items = choose(read_rows(source/'qa/items.jsonl'),per_relation)
    tasks = choose(read_rows(source/'editing/tasks.jsonl'),per_relation)
    if not items or not tasks:
        raise ValueError('The source case needs nonempty QA and Editing data')
    index_rows(items,'item_id')
    index_rows(tasks,'task_id')
    write_rows(target/'qa/items.jsonl',items)
    for form in ('open','mcq4'):
        gold = index_rows(read_rows(source/f'qa/gold_{form}.jsonl'),'item_id')
        write_rows(target/f'qa/gold_{form}.jsonl',[gold[item['item_id']] for item in items])
    write_rows(target/'editing/tasks.jsonl',tasks)
    selection = {'identity':identity,'selected_sha256':fingerprint(target),
                 'qa_items':len(items),'editing_tasks':len(tasks),
                 'qa_relation_counts':dict(Counter(item['relation_id'] for item in items)),
                 'editing_relation_counts':dict(Counter(task['relation_id'] for task in tasks))}
    write_json(marker,selection)
    return selection

def prepare_modes(case, prompt_root, modes):
    from rendering import render_case
    case, prompt_root = Path(case), Path(prompt_root)
    if any(mode != 'geojson_exact' for mode in modes):
        render_case(case)
    result = {}
    for mode in modes:
        target = prompt_root/mode
        result[mode] = {'qa':emit_qa(case,target,mode),'editing':emit_editing(case,target,mode)}
    return result

def check_public_inputs(prompts):
    forbidden = ('ground_truth','gold_answer','predicted_F0_layer','source_object_id','OSM_Geofabrik:','"oracle"')
    for path in Path(prompts).rglob('*.jsonl'):
        for row in read_rows(path):
            text = json.dumps(row['messages'],ensure_ascii=False)
            if any(token in text for token in forbidden):
                raise ValueError(f'Reference or source identifier leaked into {path.name}')

def reference_checks(case, out):
    """Gold-only diagnostics; these files never enter the inference runner."""
    from shapely import wkt
    from shapely.geometry import mapping
    from edit_core.validate import evaluate_edit
    from rendering import frame_for, to_pixels, from_pixels
    case, out = Path(case), Path(out)
    summary = {}
    for form in ('open','mcq4'):
        gold = read_rows(case/f'qa/gold_{form}.jsonl')
        predictions = out/f'qa_{form}_reference.jsonl'
        write_rows(predictions,[{'item_id':row['item_id'],'prediction':row['answer'] if form=='open' else row['answer_letter']} for row in gold])
        result = evaluate_qa(case/'qa/items.jsonl',case/f'qa/gold_{form}.jsonl',predictions,form,out/f'qa_{form}.json')
        if result['correct'] != len(gold):
            raise RuntimeError(f'{form} reference self-check failed')
        summary[form] = {'correct':result['correct'],'total':result['total']}
    tasks = read_rows(case/'editing/tasks.jsonl')
    reference_count, outside, roundtrip = 0, [], 0.0
    predictions = []
    for task in tasks:
        context = task.get('context',{})
        result = evaluate_edit(task,task['ground_truth_wkt'],extra_context=context)
        reference_count += result['score']==1
        geometry = wkt.loads(task['ground_truth_wkt'])
        frame = frame_for(task)
        pixel = to_pixels(geometry,frame)
        roundtrip = max(roundtrip,from_pixels(pixel,frame).hausdorff_distance(geometry))
        if any(value<0 or value>1536 for value in pixel.bounds):
            outside.append(task['task_id'])
        payload = {'geometry':mapping(pixel)}
        if task.get('operation')=='attribute_edit':
            layer = context.get('predicted_F0_layer',task.get('ground_truth_layer'))
            if layer is None:
                raise ValueError(f'Reference layer is missing: {task["task_id"]}')
            payload['layer'] = layer
        predictions.append({'task_id':task['task_id'],'pixel_output':payload})
    if reference_count != len(tasks) or roundtrip>1e-6:
        raise RuntimeError('Editing reference or coordinate roundtrip check failed')
    write_rows(out/'editing_pixel_reference.jsonl',predictions)
    result = evaluate_editing(case/'editing/tasks.jsonl',out/'editing_pixel_reference.jsonl',out/'editing_pixel.json',raster=True)
    if result['correct'] != len(tasks):
        raise RuntimeError('Pixel reference self-check failed')
    summary['editing'] = {'correct':reference_count,'total':len(tasks),'pixel_correct':result['correct'],
                          'max_roundtrip_metres':roundtrip,'reference_outside_canvas':len(outside),
                          'outside_examples':outside[:5],
                          'outside_policy':'Finite off-canvas coordinates are allowed; strict constraints admit alternative edits. Frames depend only on initial geometry.'}
    write_json(out/'summary.json',summary)
    return summary

def quickstart(out=None, per_relation=1, build=True):
    from verify_release import verify
    from build_release import build_city
    import run_api
    out = Path(out).resolve() if out else ROOT/'outputs'/('quickstart_'+stamp())
    if out.exists() and any(out.iterdir()):
        raise ValueError('Quickstart output must be new or empty')
    out.mkdir(parents=True,exist_ok=True)
    release_check = verify()
    example = ROOT/'examples/stockholm'
    recorded = {}
    for form in ('open','mcq4'):
        result = evaluate_qa(example/'qa/items.jsonl',example/f'qa/gold_{form}.jsonl',example/f'predictions/qa_{form}.jsonl',form,out/f'example/qa_{form}.json')
        recorded['qa_'+form] = {key:result[key] for key in ('correct','total','score','scoring_version')}
    result = evaluate_editing(example/'editing/tasks.jsonl',example/'predictions/editing.jsonl',out/'example/editing.json')
    recorded['editing'] = {key:result[key] for key in ('correct','total','score','scoring_version')}
    print('Frozen 700-item model results rescored.',flush=True)
    frozen_checks = reference_checks(example,out/'reference_checks/frozen')
    new_build = None
    if build:
        config = json.loads((ROOT/'config/stockholm.example.json').read_text(encoding='utf-8'))
        for entry in config['inputs'].values():
            entry['path'] = str((ROOT/'config'/entry['path']).resolve())
        config['output_dir'] = str(out/'new_city')
        config_path = out/'city_config.json'
        write_json(config_path,config)
        new_build = build_city(config_path)
        emit_qa(out/'new_city',out/'new_city/prompts')
        emit_editing(out/'new_city',out/'new_city/prompts')
        reference_checks(out/'new_city',out/'reference_checks/new_build')
        print('New 700 QA + 700 Editing build and reference checks passed.',flush=True)
    selection = prepare_case(example,out/'preview',per_relation)
    modes = prepare_modes(out/'preview',out/'prompts',MODES)
    check_public_inputs(out/'prompts')
    for mode in MODES:
        for name in ('qa_open','qa_mcq4','editing'):
            run_api.main(['--prompts',str(out/f'prompts/{mode}/{name}_{mode}.jsonl'),
                          '--out',str(out/f'not_used/{mode}/{name}.jsonl'),
                          '--model','DRY_RUN_ONLY','--base-url','https://example.invalid/v1','--dry-run'])
    preview_checks = reference_checks(out/'preview',out/'reference_checks/preview')
    summary = {'status':'PASS','release':(ROOT/'VERSION').read_text().strip(),'release_check':release_check,
               'example_results':recorded,'frozen_reference_checks':frozen_checks,
               'new_build':new_build,'preview_selection':selection,'preview_reference_checks':preview_checks,
               'prepared_modes':modes,'network_calls':0,'new_model_inference':False,
               'renderer':'offline_metric_v1','image_conditions_are_new_portable_inputs':True,
               'output_directory':str(out)}
    write_json(out/'verification.json',summary)
    write_json(ROOT/'outputs/latest_quickstart.json',{'output_directory':str(out),'report':str(out/'verification.json')})
    return summary

def run_model(args):
    import run_api
    out = args.out.resolve()
    modes = MODES if args.mode=='all' else (args.mode,)
    identity = {'source_sha256':fingerprint(args.case),'per_relation':args.per_relation,
                'model':args.model,'base_url':args.base_url,'api_format':args.api_format,
                'max_tokens':args.max_tokens,'temperature':args.temperature,'modes':list(modes)}
    marker = out/'run_identity.json'
    if out.exists() and any(out.iterdir()):
        if not args.resume or not marker.exists() or json.loads(marker.read_text(encoding='utf-8')) != identity:
            raise ValueError('Output is not empty; --resume requires unchanged inputs and run settings')
    write_json(marker,identity)
    selection = prepare_case(args.case,out/'case',args.per_relation)
    prepare_modes(out/'case',out/'prompts',modes)
    check_public_inputs(out/'prompts')
    reports = {}
    for mode in modes:
        reports[mode] = {}
        for name in ('qa_open','qa_mcq4','editing'):
            prediction = out/f'predictions/{mode}/{name}.jsonl'
            command = ['--prompts',str(out/f'prompts/{mode}/{name}_{mode}.jsonl'),
                       '--out',str(prediction),'--model',args.model,'--base-url',args.base_url,
                       '--api-format',args.api_format,'--key-env',args.key_env,'--max-tokens',str(args.max_tokens)]
            if args.temperature is not None:
                command += ['--temperature',str(args.temperature)]
            if args.resume:
                command.append('--resume')
            if args.dry_run:
                command.append('--dry-run')
            if run_api.main(command):
                raise RuntimeError(f'Inference failed for {mode}/{name}; keep output and rerun with --resume')
            if not args.dry_run:
                if name=='editing':
                    result = evaluate_editing(out/'case/editing/tasks.jsonl',prediction,out/f'reports/{mode}/{name}.json',raster=mode=='map_annotated')
                else:
                    form = name.split('_',1)[1]
                    result = evaluate_qa(out/'case/qa/items.jsonl',out/f'case/qa/gold_{form}.jsonl',prediction,form,out/f'reports/{mode}/{name}.json',mode)
                reports[mode][name] = {key:result[key] for key in ('metric','correct','total','score','scoring_version')}
    summary = {'status':'DRY_RUN' if args.dry_run else 'PASS','model':args.model,'selection':selection,
               'modes':list(modes),'renderer':'offline_metric_v1','reports':reports,
               'requests':len(modes)*(2*selection['qa_items']+selection['editing_tasks']),
               'new_model_inference':not args.dry_run}
    write_json(out/'summary.json',summary)
    return summary
