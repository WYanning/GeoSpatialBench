"""Emit model-only inputs. References remain in separate QA gold and editing task files."""
import json
import re
from pathlib import Path

from io_utils import read_rows, write_rows, ROOT, sha256

def public_edit_payload(task):
    from edit_core.protocol import build_model_input
    payload = json.loads(build_model_input(task))
    source_ids = [str(f.get('source_object_id','')) for f in task['scene_features'] if f.get('source_object_id')]
    payload['instruction'] = re.sub(r'\([^)]*\)',lambda m:'' if any(s in m.group() for s in source_ids) else m.group(),payload['instruction'])
    # Historical instructions include parenthetical source IDs solely for human auditing.
    payload['instruction'] = re.sub(r'\s*\((?:Stockholm|Paris|Nairobi|Hong Kong|Wuhan|Detroit|v5)\s[^)]*\)\.?', '', payload['instruction'])
    payload['instruction'] = re.sub(r'\s*\([^)]*(?:gis_osm_|OSM_Geofabrik:)[^)]*\)\.?','',payload['instruction'])
    for feature in payload['scene_features_geojson']:
        layer = feature.get('semantics',{}).get('layer')
        feature['semantics'] = {'layer':layer} if layer is not None else {}
    return payload

def choose(rows, per_relation):
    if not per_relation:
        return rows
    from collections import Counter
    counts = Counter()
    result = []
    for row in rows:
        rid = row['relation_id']
        if counts[rid]<per_relation:
            result.append(row)
            counts[rid]+=1
    return result

def emit_qa(case, out, mode='geojson_exact', per_relation=0):
    from qa_core import build_l1_full700 as core
    items = choose(read_rows(Path(case)/'qa/items.jsonl'),per_relation)
    all_items = read_rows(Path(case)/'qa/items.jsonl')
    item_indices = {item['item_id']:i for i,item in enumerate(all_items)}
    # MCQ alternatives must match the released file, rather than be regenerated with a new seed.
    mcq = {g['item_id']:g for g in read_rows(Path(case)/'qa/gold_mcq4.jsonl')}
    for form in ('open','mcq4'):
        rows = []
        for item in items:
            row = core.NT.prompt_row(item,'geojson_exact')
            text = row['messages'][0]['content']
            text = re.sub(r'CRS: [^;\n]+', 'CRS: '+item['crs'], text, count=1)
            if form == 'mcq4':
                g = mcq[item['item_id']]
                index = item_indices[item['item_id']]
                if item['relation_id'].startswith('T'):
                    mcq_task = core.topology_mcq(item,index)[0]
                else:
                    mcq_task = core.FORMS.non_topology_mcq(item,index)[0]
                options = g.get('options')
                if options is None:
                    raise ValueError('MCQ gold must include options')
                option_text = '\n'.join(f'{key}. {value}' for key,value in options.items()) if isinstance(options,dict) else '\n'.join(f'{chr(65+i)}. {value}' for i,value in enumerate(options))
                text = text.split('\nAnswer format:')[0]+'\nOptions:\n'+option_text+'\nReturn one option letter only.'
                text = text.replace('Task: '+item['task'],'Task: '+mcq_task,1)
            if mode in ('map_annotated','hybrid_geojson_exact'):
                if mode == 'map_annotated':
                    text = text.split('\nInput (')[0]
                    if form == 'mcq4':
                        text += '\nOptions:\n'+option_text+'\nReturn one option letter only.'
                    else:
                        text += '\nAnswer format: '+item['answer_format']+'\nReturn only the answer.'
                    text += '\nUse the attached annotated map.'
                else:
                    text += '\nThe attached map depicts the same scene.'
            prompt = {'prompt_id':f"{item['item_id']}--{form}--{mode}", 'item_id':item['item_id'],
                      'relation_id':item['relation_id'],'question_form':form,'representation_mode':mode,
                      'messages':[{'role':'user','content':text}]}
            if mode != 'geojson_exact':
                image_path = Path(case).resolve()/'images/qa'/f"{item['item_id']}.png"
                if not image_path.exists():
                    raise FileNotFoundError(f'Render this scene first: {image_path}')
                from rendering import image_metadata
                metadata = image_metadata(image_path,item,False)
                import os
                prompt['image'] = os.path.relpath(image_path,Path(out).resolve()).replace('\\','/')
                prompt.update(image_sha256=metadata['image_sha256'],renderer=metadata['renderer'],
                              frame_sha256=sha256(image_path.with_suffix('.frame.json')))
            rows.append(prompt)
        write_rows(Path(out)/f'qa_{form}_{mode}.jsonl',rows)
    return {'items':len(items),'forms':2,'mode':mode,'full_city_items':len(all_items)}

def emit_editing(case, out, mode='geojson_exact', per_relation=0):
    from edit_core.protocol import SYSTEM_PROMPT
    tasks = choose(read_rows(Path(case)/'editing/tasks.jsonl'),per_relation)
    rows = []
    for task in tasks:
        payload = public_edit_payload(task)
        system = SYSTEM_PROMPT
        if mode == 'map_annotated':
            payload = {k:payload[k] for k in ('instruction','operation','target_feature_id','constraints')}
            payload['pixel_frame'] = '1536 x 1536; x increases rightwards, y increases downwards; vertices use image pixels. Finite coordinates outside the canvas are allowed when the edit moves off the displayed map.'
            system = ('Edit only F0 in the annotated image. Return only JSON: '
                      '{"geometry":{"type":"Point|LineString|Polygon","coordinates":<pixel coordinates>},"layer":<optional O06 layer>}. '
                      'For O06 return the unchanged F0 geometry and the new layer. Do not include reference features.')
        else:
            payload['coordinate_units'] = 'metres in the local Cartesian task frame'
        user = json.dumps(payload,ensure_ascii=False,separators=(',',':'))
        row = {'prompt_id':f"{task['task_id']}--{mode}",'task_id':task['task_id'],
               'relation_id':task['relation_id'],'representation_mode':mode,
               'output_protocol':'pixel_geometry' if mode == 'map_annotated' else 'transform_json',
               'messages':[{'role':'system','content':system},{'role':'user','content':user}]}
        if mode != 'geojson_exact':
            import os
            image_path = Path(case).resolve()/'images/editing'/f"{task['task_id']}.png"
            if not image_path.exists():
                raise FileNotFoundError(f'Render this scene first: {image_path}')
            from rendering import image_metadata
            metadata = image_metadata(image_path,task,True)
            row['image'] = os.path.relpath(image_path,Path(out).resolve()).replace('\\','/')
            row.update(image_sha256=metadata['image_sha256'],renderer=metadata['renderer'],
                       frame_sha256=sha256(image_path.with_suffix('.frame.json')))
        rows.append(row)
    write_rows(Path(out)/f'editing_{mode}.jsonl',rows)
    return {'tasks':len(tasks),'mode':mode}
