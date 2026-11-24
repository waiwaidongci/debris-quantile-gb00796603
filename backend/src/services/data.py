import csv
import hashlib
import io
import json
import math
import time
from pathlib import Path

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from src.models.database import add_event, engine
from src.models.tables import datasets

PROFILE = json.loads(Path(__file__).with_name('domain.json').read_text(encoding='utf-8'))


def convert(value, field):
    if value is None or isinstance(value, bool) or str(value).strip() == '':
        raise ValueError('不能为空')
    kind = field['type']
    if kind == 'str':
        return str(value).strip()
    if kind == 'select':
        if value not in field['choices']:
            raise ValueError('请选择有效选项')
        return value
    number = int(str(value)) if kind == 'int' else float(value)
    if not math.isfinite(number):
        raise ValueError('必须为有限数值')
    if number < field.get('min', -math.inf) or number > field.get('max', math.inf):
        raise ValueError('数值超出可接受范围')
    return number


def csv_records(text):
    reader = csv.DictReader(io.StringIO(text.lstrip('\ufeff'), newline=''), strict=True)
    columns = PROFILE['columns']
    try:
        fieldnames = reader.fieldnames
    except csv.Error as error:
        raise ValueError('CSV 表头的引号格式有误。') from error
    required = {field['key'] for field in columns}
    if not fieldnames or len(fieldnames) != len(required) or set(fieldnames) != required:
        raise ValueError('CSV 表头须为 ' + '、'.join(field['key'] for field in columns))
    rows = []
    try:
        for row in reader:
            if None in row:
                raise ValueError(f'第 {reader.line_num} 行列数多于表头。')
            normalized = {}
            for field in columns:
                try:
                    normalized[field['key']] = convert(row[field['key']], field)
                except (ValueError, TypeError, OverflowError) as error:
                    raise ValueError(f"第 {reader.line_num} 行 {field['label']} 无效（{error}）。") from error
            rows.append(normalized)
            if len(rows) > 5000:
                raise ValueError('单份数据最多 5000 行。')
    except csv.Error as error:
        raise ValueError('CSV 引号或字段格式有误。') from error
    if not rows:
        raise ValueError('CSV 需要至少一条数据。')
    return rows


def parse_options(values):
    fields = PROFILE['options']
    reserved = {'work_date', 'job_date', 'day', 'date',
                'scenario_dataset_id', 'scenario_id', 'scenarios_id',
                'scenario_revision_id', 'hours_dataset_id',
                'team_target_pct', 'scenario_target_pct', 'coverage_target_pct',
                'team_coverage_target_pct', 'scenario_coverage_target_pct',
                'coverage_target', 'team_coverage_target', 'scenario_coverage_target'}
    incoming = dict(values or {})
    # Pinned references live in their own columns; keep them out of numeric option validation.
    for key in reserved & set(incoming):
        incoming.pop(key, None)
    if set(incoming) - {field['key'] for field in fields}:
        raise ValueError('存在未定义的分析参数。')
    output = {}
    for field in fields:
        try:
            output[field['key']] = convert(values.get(field['key'], field['default']), field)
        except (ValueError, TypeError, OverflowError) as error:
            raise ValueError(f"{field['label']} 无效（{error}）。") from error
    for key in reserved & set(values or {}):
        output[key] = values[key]
    return output


def save_revision(name, records, owner, kind='orders'):
    if not name.strip():
        raise ValueError('数据名称不能为空。')
    kind = kind if kind in ('orders', 'scenarios') else 'orders'
    payload = json.dumps(records, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    checksum = hashlib.sha256(payload.encode()).hexdigest()
    now = time.time()
    existing = select(datasets.c.id).where(datasets.c.checksum == checksum)
    try:
        with engine.begin() as connection:
            found = connection.scalar(existing)
            if found is not None:
                return found, False
            key = connection.execute(insert(datasets).values(name=name.strip(), owner=owner, records=records,
                                                             checksum=checksum, kind=kind,
                                                             created=now)).inserted_primary_key[0]
            add_event(connection, owner, 'import', key, now)
            return key, True
    except IntegrityError:
        with engine.connect() as connection:
            found = connection.scalar(existing)
            if found is None:
                raise
            return found, False
