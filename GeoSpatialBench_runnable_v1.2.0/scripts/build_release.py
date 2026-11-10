"""Build independent QA and editing pools using one portable city configuration."""
from collections import Counter
import json
from pathlib import Path
import shutil
import tempfile

from io_utils import read_rows, write_rows, write_json

def load_config(config_path):
    from qa_core.build_city_case import read_json, validate_config, read_role, data_diagnostics, assert_data_ready
    config_path = Path(config_path).resolve()
    config = read_json(config_path)
    if config.get('seed') is None:
        raise ValueError('Set an integer seed; historical Stockholm seed is unknown and cannot be reconstructed')
    crs, out = validate_config(config, config_path)
    frames = {role:read_role(role,config,config_path,crs) for role in ('pois','roads','buildings','landuse')}
    diagnostics = data_diagnostics(frames)
    assert_data_ready(diagnostics)
    for role, frame in frames.items():
        if frame['_template_row_id'].duplicated().any():
            # A source multipart geometry may have more than one exploded component.
            frame['_template_row_id'] = frame['_template_row_id'] + ':part-' + frame.index.astype(str)
    return config, crs, out, frames, diagnostics

def editing_pools(frames, config):
    """Build a second feature pool directly from vectors, independently of QA scenes."""
    import geopandas as gpd
    from edit_core.pools import cap_pools
    role_kind = {'buildings':'building','landuse':'green','roads':'road','pois':'point'}
    pools = []
    for role in ('buildings','landuse','roads','pois'):
        rows = []
        for _, row in frames[role].iterrows():
            label = role_kind[role]
            rows.append({'fid':str(row['_template_source_name'])+':'+str(row['_template_row_id']),
                         'kind':label,'semantics':{'category':label,'role':label,'label':label},
                         'geometry':row.geometry})
        pools.append(gpd.GeoDataFrame(rows, geometry='geometry',crs=frames[role].crs))
    return cap_pools(tuple(pools),max_buildings=8000,max_green=4000,max_roads=6000,max_points=12000,
                     seed=int(config['seed']))

def mine_editing(frames, config, per_relation=25):
    from edit_core.miner import CityEditMiner
    from edit_core.factory import CORE_28_IDS
    from edit_core.validate import evaluate_edit
    miner = CityEditMiner(editing_pools(frames,config),target=per_relation,seed=int(config['seed']),
                          city=config['city_name'],f0_cap=None,
                          task_id_prefix=config['id_prefix'].upper()+'-ED-')
    miner.run_all()
    for _ in range(8):
        short = [rid for rid in CORE_28_IDS if miner.counts[rid]<per_relation]
        if not short:
            break
        before = len(miner.tasks)
        miner.f0_cap = None
        for rid in short:
            getattr(miner,'gen_'+rid)()
        if len(miner.tasks)==before:
            break
    short = {rid:miner.counts[rid] for rid in CORE_28_IDS if miner.counts[rid]!=per_relation}
    if short:
        raise RuntimeError(f'Editing candidates insufficient (required {per_relation} per relation): {short}; enlarge the city clip or adjust filters')
    tasks = []
    for task in miner.tasks:
        task = {k:v for k,v in task.items() if not k.startswith('_')}
        task['crs_source'] = config['target_crs']
        task['coordinate_system'] = 'local Cartesian metres; translated from projected source geometry'
        # Explicitly use the reference layer only for checking a reference edit.
        reference_context = task.get('context',{})
        ev = evaluate_edit(task,task['ground_truth_wkt'],extra_context=reference_context)
        if ev['score']!=1:
            raise RuntimeError(f"Editing reference fails its constraints: {task['task_id']}")
        tasks.append(task)
    tasks.sort(key=lambda t:(CORE_28_IDS.index(t['relation_id']),t['task_id']))
    return tasks

def build_city(config_path, task='both', editing_per_relation=25):
    from qa_core.build_city_case import build as build_qa, resolve_path
    config_path = Path(config_path).resolve()
    config, crs, out, frames, diagnostics = load_config(config_path)
    if out.exists() and any(out.iterdir()):
        raise ValueError(f'Output directory is not empty: {out}; choose a new output_dir')
    out.mkdir(parents=True,exist_ok=True)
    report = {'city':config['city_name'],'seed':config['seed'],'crs':crs.to_string(),
              'status':'BUILDING','data_diagnostics':diagnostics,'qa_editing_instances_paired':False}
    write_json(out/'build_report.json',report)
    if task in ('qa','both'):
        # Legacy core emits diagnostic modes for its audit. Retain only the public essentials.
        with tempfile.TemporaryDirectory(prefix='.geospatialbench-',dir=out) as temp:
            temp = Path(temp)
            normalized = json.loads(json.dumps(config))
            normalized['output_dir'] = str(temp/'qa')
            for entry in normalized['inputs'].values():
                entry['path'] = str(resolve_path(entry['path'],config_path))
            write_json(temp/'city.json',normalized)
            qa_report = build_qa(temp/'city.json')
            (out/'qa').mkdir()
            shutil.copyfile(temp/'qa/data/l1_full700_items_700.jsonl',out/'qa/items.jsonl')
            for form in ('open','mcq4'):
                shutil.copyfile(temp/f'qa/preds/l1_full700_{form}_private_gold_700.jsonl',out/f'qa/gold_{form}.jsonl')
            report['qa'] = {'items':700,'relations':28,'audit_status':qa_report['status'],
                            'geometry_balance':qa_report['build'].get('geometry_balance')}
            # The audit includes oracle recomputation, serialized geometry, and prompt leakage checks.
            shutil.copyfile(temp/'qa/reports/l1_full700_audit.json',out/'qa/audit.json')
            report['qa']['geometry_balance'] = json.loads((out/'qa/audit.json').read_text(encoding='utf-8')).get('geometry_balance')
    if task in ('editing','both'):
        tasks = mine_editing(frames,config,editing_per_relation)
        write_rows(out/'editing/tasks.jsonl',tasks)
        report['editing'] = {'tasks':len(tasks),'reference_constraints_pass':len(tasks),
                             'by_relation':dict(Counter(t['relation_id'] for t in tasks))}
    report['status'] = 'PASS'
    write_json(out/'build_report.json',report)
    return report
