#!/usr/bin/env python3
"""Run public prompts against a user-selected compatible endpoint. No keys in files."""
import argparse
import base64
import copy
import json
import os
from pathlib import Path
import sys
import time
import urllib.request
import urllib.error

from io_utils import read_rows, index_rows, write_json, sha256

def request_messages(row, prompt_file):
    """Only messages and the optional image are sent; other row fields are metadata."""
    messages = copy.deepcopy(row['messages'])
    if not isinstance(messages,list) or not messages or any(not isinstance(m,dict) or m.get('role') not in ('system','user','assistant') or not isinstance(m.get('content'),str) for m in messages):
        raise ValueError('Each message needs a supported role and string content')
    if row.get('image'):
        path = (Path(prompt_file).resolve().parent/row['image']).resolve()
        if path.suffix.lower() not in ('.png','.jpg','.jpeg','.webp'):
            raise ValueError('Only PNG, JPEG and WebP image inputs are supported')
        if row.get('image_sha256') and sha256(path) != row['image_sha256']:
            raise ValueError(f'Image changed after prompt preparation: {path.name}')
        if row.get('frame_sha256') and sha256(path.with_suffix('.frame.json')) != row['frame_sha256']:
            raise ValueError(f'Image coordinate metadata changed: {path.name}')
        mime = {'.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.webp':'image/webp'}[path.suffix.lower()]
        url = 'data:'+mime+';base64,'+base64.b64encode(path.read_bytes()).decode('ascii')
        for message in reversed(messages):
            if message['role']=='user':
                message['content'] = [{'type':'text','text':message['content']},
                                      {'type':'image_url','image_url':{'url':url}}]
                break
    return messages

def payload_for(messages, args):
    if args.api_format=='chat':
        payload = {'model':args.model,'messages':messages,'max_tokens':args.max_tokens}
    else:
        inputs = []
        for message in messages:
            content = message['content']
            if isinstance(content,list):
                content = [{'type':'input_text','text':p['text']} if p['type']=='text' else
                           {'type':'input_image','image_url':p['image_url']['url']} for p in content]
            inputs.append({'role':message['role'],'content':content})
        payload = {'model':args.model,'input':inputs,'max_output_tokens':args.max_tokens}
    if args.temperature is not None:
        payload['temperature'] = args.temperature
    return payload

def generate(messages, args, api_key):
    suffix = '/chat/completions' if args.api_format=='chat' else '/responses'
    endpoint = args.base_url.rstrip('/')+suffix
    req = urllib.request.Request(endpoint,json.dumps(payload_for(messages,args)).encode('utf-8'),
                                 {'Content-Type':'application/json','Authorization':'Bearer '+api_key})
    for attempt in range(args.transport_retries+1):
        try:
            with urllib.request.urlopen(req,timeout=args.timeout) as response:
                result = json.load(response)
            if args.api_format=='chat':
                raw = result['choices'][0]['message'].get('content') or ''
                if isinstance(raw,list):
                    raw = '\n'.join(p.get('text','') for p in raw)
            else:
                raw = result.get('output_text') or '\n'.join(p.get('text','') for item in result.get('output',[])
                                                              for p in item.get('content',[]) if p.get('type')=='output_text')
            return raw,result.get('usage',{})
        except urllib.error.HTTPError as exc:
            if exc.code not in (408,429,500,502,503,504) or attempt==args.transport_retries:
                # Never include headers, keys, or provider body in an error log.
                raise RuntimeError(f'Endpoint returned HTTP {exc.code}') from exc
        except (urllib.error.URLError,TimeoutError) as exc:
            if attempt==args.transport_retries:
                raise RuntimeError('Endpoint transport error or timeout') from exc
        time.sleep(min(2**attempt,8))

def needs_edit_retry(row, raw):
    if row.get('output_protocol')=='pixel_geometry':
        from shapely.geometry import shape
        from edit_core.protocol import extract_json_obj
        try:
            geom = shape(extract_json_obj(raw)['geometry'])
            return geom.is_empty or not geom.is_valid
        except Exception:
            return True
    if row.get('output_protocol')!='transform_json':
        return False
    from shapely.geometry import shape
    from evaluation import decode_edit
    context = json.loads(next(m['content'] for m in row['messages'] if m['role']=='user'))
    task = {'operation':context['operation'],
            'scene_features':[{'feature_id':f['feature_id'],'wkt':shape(f['geojson']).wkt} for f in context['scene_features_geojson']]}
    try:
        geometry,extra,status = decode_edit(task,{'prediction':raw})
        return geometry is None
    except Exception:
        return True

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--prompts',required=True,type=Path)
    ap.add_argument('--out',required=True,type=Path)
    ap.add_argument('--model',required=True)
    ap.add_argument('--base-url',required=True,help='API root including /v1 where required')
    ap.add_argument('--api-format',choices=['chat','responses'],default='chat')
    ap.add_argument('--key-env',default='BENCHMARK_API_KEY')
    ap.add_argument('--max-tokens',type=int,default=4096)
    ap.add_argument('--temperature',type=float,default=None,help='Omit by default for provider compatibility')
    ap.add_argument('--timeout',type=float,default=180)
    ap.add_argument('--transport-retries',type=int,default=2)
    ap.add_argument('--limit',type=int,default=0)
    ap.add_argument('--resume',action='store_true')
    ap.add_argument('--dry-run',action='store_true',help='Validate requests locally; make no network calls')
    args = ap.parse_args(argv)
    rows = read_rows(args.prompts)
    index_rows(rows,'prompt_id')
    if args.limit<0 or args.max_tokens<1 or args.transport_retries<0 or args.timeout<=0 or not rows:
        ap.error('Invalid limit/token/retry count or empty prompt file')
    selected = rows[:args.limit] if args.limit else rows
    if args.dry_run:
        for row in selected:
            payload_for(request_messages(row,args.prompts),args)
        print(json.dumps({'dry_run':True,'requests_validated':len(selected),'network_calls':0}))
        return 0
    api_key = os.environ.get(args.key_env)
    if not api_key:
        ap.error(f'Set the {args.key_env} environment variable first')
    args.out.parent.mkdir(parents=True,exist_ok=True)
    manifest = args.out.with_suffix('.run.json')
    identity = {'prompts_sha256':sha256(args.prompts),'model':args.model,'base_url':args.base_url,
                'api_format':args.api_format,'max_tokens':args.max_tokens,'temperature':args.temperature}
    identity['images_sha256'] = {row['prompt_id']:sha256((args.prompts.resolve().parent/row['image']).resolve())
                                for row in rows if row.get('image')}
    existing = {}
    if args.out.exists():
        if not args.resume:
            ap.error('Output already exists; choose a new file or use --resume')
        if not manifest.exists() or json.loads(manifest.read_text(encoding='utf-8'))!=identity:
            ap.error('Resume requires the same prompt file, model, endpoint and generation settings')
        existing = index_rows(read_rows(args.out),'prompt_id')
        if set(existing)-{r['prompt_id'] for r in rows}:
            ap.error('Existing output includes unknown prompt IDs')
    write_json(manifest,identity)
    failures = 0
    error_file = args.out.with_suffix('.errors.jsonl')
    with args.out.open('a',encoding='utf-8') as output:
        for i,row in enumerate(selected,1):
            if row['prompt_id'] in existing:
                continue
            try:
                messages = request_messages(row,args.prompts)
                raw,usage = generate(messages,args,api_key)
                attempts = 1
                history = [{'raw_response':raw,'usage':usage}]
                if needs_edit_retry(row,raw):
                    messages.append({'role':'assistant','content':raw})
                    messages.append({'role':'user','content':'The output could not be decoded as a valid edit or did not change F0. Return one valid nonzero edit in the requested JSON format. No constraint-satisfaction feedback is provided.'})
                    raw,usage_retry = generate(messages,args,api_key)
                    history.append({'raw_response':raw,'usage':usage_retry})
                    usage = {'first_attempt':usage,'retry':usage_retry}
                    attempts = 2
                record = {k:row[k] for k in ('prompt_id','item_id','task_id','question_form','relation_id','representation_mode') if k in row}
                record.update(prediction=raw,raw_response=raw,model=args.model,attempts=attempts,usage=usage,attempt_history=history)
                record.update({key:row[key] for key in ('image_sha256','frame_sha256','renderer') if key in row})
                output.write(json.dumps(record,ensure_ascii=False,allow_nan=False)+'\n')
                output.flush()
            except Exception as exc:
                failures += 1
                with error_file.open('a',encoding='utf-8') as errors:
                    errors.write(json.dumps({'prompt_id':row['prompt_id'],'error':str(exc)})+'\n')
            print(f'{i}/{len(selected)} processed; transport failures={failures}',flush=True)
    return 1 if failures else 0

if __name__=='__main__':
    raise SystemExit(main())
