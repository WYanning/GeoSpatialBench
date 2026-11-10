"""Full-denominator QA and editing evaluation; incomplete runs cannot inflate scores."""
from collections import defaultdict
from pathlib import Path

from io_utils import read_rows, index_rows, write_json, write_rows, sha256

def coverage(gold, predictions, key, allow_partial):
    pred = index_rows(predictions, key)
    extra = sorted(set(pred)-set(gold))
    missing = sorted(set(gold)-set(pred))
    if extra:
        raise ValueError(f'Unknown prediction IDs: {extra[:5]}')
    if missing and not allow_partial:
        raise ValueError(f'Missing {len(missing)} predictions; use --allow-partial to count missing outputs as failures')
    return pred, missing

def stats(details):
    def aggregate(rows):
        n = len(rows)
        return {'correct':sum(bool(r['correct']) for r in rows), 'total':n,
                'score':sum(bool(r['correct']) for r in rows)/n if n else None}
    result = aggregate(details)
    for key in ['relation_id', 'relation_group']:
        groups = defaultdict(list)
        for row in details:
            groups[row[key]].append(row)
        result['by_'+key] = {k:aggregate(v) for k,v in sorted(groups.items())}
    return result

def save(report, details, out):
    write_json(out, report)
    write_rows(Path(out).with_suffix('.details.jsonl'), details)
    return report

def evaluate_qa(items_path, gold_path, predictions_path, form, out, mode='geojson_exact', allow_partial=False, scoring='current'):
    from qa_core.scoring import score_open, score_mcq, extraction_audit, SCORING_VERSION, LEGACY_SCORING_VERSION
    if scoring not in ('current', 'legacy'):
        raise ValueError('QA scoring must be current or legacy')
    version = LEGACY_SCORING_VERSION if scoring == 'legacy' else SCORING_VERSION
    items = index_rows(read_rows(items_path), 'item_id')
    gold = index_rows(read_rows(gold_path), 'item_id')
    if not gold or set(gold) != set(items):
        raise ValueError('QA gold and items must have the same nonempty ID set')
    pred, missing = coverage(gold, read_rows(predictions_path), 'item_id', allow_partial)
    details = []
    for item_id, g in gold.items():
        item = items[item_id]
        raw = pred.get(item_id, {}).get('prediction', '')
        ok, parsed = (score_open(raw,g,legacy=scoring=='legacy') if form == 'open' else score_mcq(raw,g))
        sufficient_mode = 'geojson_exact' if mode == 'hybrid_geojson_exact' else mode
        sufficient = item.get('mode_information_sufficient',{}).get(sufficient_mode)
        if mode in ('map_annotated','hybrid_geojson_exact'):
            sufficient = True  # Offline map renderer includes required layer cards.
        if sufficient is None:
            raise ValueError(f'Unknown representation mode: {mode}')
        details.append({'item_id':item_id,'relation_id':item['relation_id'],
                        'relation_group':item['relation_id'][0], 'correct':bool(ok),
                        'parsed_prediction':parsed, 'information_sufficient':sufficient,
                        'missing_prediction':item_id not in pred, **extraction_audit(raw,g,form,legacy=scoring=='legacy'), 'scoring_version':version})
    report = {'task':'QA','form':form,'mode':mode,'metric':'accuracy','scoring_version':version,
              'complete':not missing,'missing_predictions':len(missing), **stats(details),
              'information_sufficient':stats([r for r in details if r['information_sufficient']]),
              'source_sha256':{'items':sha256(items_path),'gold':sha256(gold_path),'predictions':sha256(predictions_path)}}
    assessed = [r for r in details if r.get('format_compliant') is not None]
    report['format_compliance'] = {'assessed':len(assessed),
                                  'compliant':sum(r['format_compliant'] is True for r in assessed)}
    return save(report, details, out)

def decode_edit(task, row):
    from edit_core.protocol import _f0_and_scene, apply_transform, decode_transform, is_identity
    f0, scene = _f0_and_scene(task)
    spec = row.get('transform')
    if isinstance(spec, dict):
        geometry, spec, status = apply_transform(f0, spec, scene)
    elif row.get('prediction_wkt'):
        from shapely import wkt
        geometry = wkt.loads(row['prediction_wkt'])
        spec = None
        status = 'wkt'
    else:
        geometry, spec, status = decode_transform(row.get('prediction',row.get('raw_response','')),f0,scene)
    # The original reference context includes a known-good layer. Never use it for a prediction.
    context = {'predicted_F0_layer': spec['layer']} if spec and spec.get('op')=='attribute_edit' and spec.get('layer') is not None else {}
    if geometry is None or not geometry.is_valid or geometry.is_empty:
        return None, context, status
    if spec is not None and is_identity(task_op=task.get('operation',''),spec=spec,before=f0,after=geometry):
        return None, context, 'identity'
    return geometry, context, status

def evaluate_editing(tasks_path, predictions_path, out, allow_partial=False, raster=False):
    from shapely.errors import ShapelyError
    from edit_core.validate import evaluate_edit
    tasks = index_rows(read_rows(tasks_path), 'task_id')
    if not tasks:
        raise ValueError('Empty editing task file')
    rows = read_rows(predictions_path)
    for row in rows:
        if 'task_id' not in row and 'id' in row:
            row['task_id'] = row['id']
    pred, missing = coverage(tasks, rows, 'task_id', allow_partial)
    details = []
    for task_id, task in tasks.items():
        row = pred.get(task_id, {})
        try:
            if raster:
                from rendering import decode_pixel_edit
                geometry, context, pixel_error = decode_pixel_edit(task, row)
                status = 'pixel_geometry'
            else:
                geometry, context, status = decode_edit(task, row)
                pixel_error = None
            ev = evaluate_edit(task,geometry.wkt,extra_context=context) if geometry is not None else {'score':0.0}
            strict = ev['score'] == 1.0
            correct = strict
            if raster and geometry is not None and not strict and pixel_error is not None and pixel_error <= 4:
                from shapely import wkt
                initial = wkt.loads(next(f['wkt'] for f in task['scene_features'] if f['feature_id']=='F0'))
                attribute_ok = task.get('operation') != 'attribute_edit' or bool(ev.get('constraints_satisfied'))
                changed = task.get('operation') == 'attribute_edit' or not initial.equals(geometry)
                correct = geometry.is_valid and not geometry.is_empty and attribute_ok and changed
            error = None
        except (ValueError, TypeError, KeyError, OverflowError, ShapelyError) as exc:
            ev, status, correct, strict, pixel_error = {'score':0.0}, 'invalid_output', False, False, None
            error = str(exc)
        details.append({'task_id':task_id,'relation_id':task['relation_id'],'relation_group':task['relation_id'][0],
                        'correct':bool(correct),'strict_CSR':strict,'status':status,'pixel_hausdorff':pixel_error,
                        'missing_prediction':task_id not in pred,'evaluation':ev,'error':error})
    return save({'task':'Editing','metric':'raster_tolerant_success' if raster else 'strict_CSR',
                 'scoring_version':'explicit_direction_and_declared_tolerance_20260910_no_gold_layer',
                 'complete':not missing,'missing_predictions':len(missing), **stats(details),
                 'source_sha256':{'tasks':sha256(tasks_path),'predictions':sha256(predictions_path)}},details,out)
