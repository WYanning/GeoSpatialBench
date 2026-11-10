"""Small, strict JSONL helpers shared by the public commands."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def read_rows(path):
    path = Path(path)
    rows = []
    with path.open(encoding='utf-8-sig') as stream:
        for line_no, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError as exc:
                raise ValueError(f'{path}:{line_no}: invalid JSON') from exc
            if not isinstance(row, dict):
                raise ValueError(f'{path}:{line_no}: expected a JSON object')
            rows.append(row)
    return rows

def index_rows(rows, key):
    result = {}
    for row in rows:
        if not isinstance(row.get(key), str) or not row[key]:
            raise ValueError(f'Each row needs a nonempty string {key}')
        if row[key] in result:
            raise ValueError(f'Duplicate {key}: {row[key]}')
        result[row[key]] = row
    return result

def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)+'\n', encoding='utf-8')

def write_rows(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8') as stream:
        for row in rows:
            stream.write(json.dumps(row,ensure_ascii=False,separators=(',',':'),allow_nan=False)+'\n')

def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
