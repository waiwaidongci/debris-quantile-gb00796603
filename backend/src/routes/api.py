from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from src.config.settings import ARTIFACTS
from src.constants.enums import Role
from src.controllers.query import delivery_status, jobs, recent_activity, require_row, revisions
from src.models.tables import datasets, runs
from src.schemas.requests import Delivery, Import, Login, Review, Run, ScenarioImport
from src.services.data import PROFILE, csv_records, save_revision
from src.services.domain import (
    normalize_scenarios,
    sample_rows,
    team_assignment,
    to_long_scenario_records,
)
from src.services.workflow import add_run, cancel_schedule, review_run, schedule_report
from src.utils.exports import write_csv
from src.utils.security import allowed, authenticate, signed_in

router = APIRouter()
analyst = allowed(Role.analyst.value)
reviewer = allowed(Role.reviewer.value)

ORDER_HEADER_SET = {field['key'] for field in PROFILE['columns']}


def scenario_records_from_payload(payload):
    return to_long_scenario_records(normalize_scenarios(payload))


def _declared_kind(body):
    kind = getattr(body, 'kind', None)
    if kind:
        return kind
    extra = getattr(body, 'model_extra', None) or {}
    for key in ('type', 'data_kind', 'dataset_type', 'source', 'category'):
        value = extra.get(key)
        is_scenario = isinstance(value, str) and (
            'scenario' in value.lower() or value in ('情景', '情景工时', '逐情景工时'))
        if is_scenario:
            return 'scenarios'
        if isinstance(value, str) and value in ('orders', 'order', '工单', '台账'):
            return 'orders'
    return None


def parse_import(body):
    """Return (records, kind) for either the order ledger or scenario work hours."""
    declared = _declared_kind(body)
    if getattr(body, 'rows', None) is not None and not getattr(body, 'csv', None):
        rows = body.rows
        if declared == 'orders':
            return csv_records_from_json(rows), 'orders'
        if declared == 'scenarios':
            return scenario_records_from_payload(rows), 'scenarios'
        try:
            return csv_records_from_json(rows), 'orders'
        except ValueError:
            return scenario_records_from_payload(rows), 'scenarios'

    text = body.csv or ''
    if declared == 'scenarios':
        return scenario_records_from_payload(read_csv_dicts(text)), 'scenarios'
    if declared == 'orders':
        return csv_records(text), 'orders'

    cleaned = text.lstrip('﻿')
    first_line = cleaned.splitlines()[0] if cleaned.strip() else ''
    try:
        import csv as _csv
        import io as _io
        headers = next(_csv.reader(_io.StringIO(first_line)))
    except Exception:
        headers = []
    header_set = {item.strip() for item in headers}
    if header_set == ORDER_HEADER_SET:
        return csv_records(text), 'orders'
    # Only auto-route when there is an unambiguous scenario/grouping column;
    # otherwise preserve the strict order-ledger validation and its messages.
    if has_strong_scenario_marker(headers):
        return scenario_records_from_payload(read_csv_dicts(text)), 'scenarios'
    return csv_records(text), 'orders'


def read_csv_dicts(text):
    import csv as _csv
    import io as _io
    return list(_csv.DictReader(_io.StringIO((text or '').lstrip('﻿'), newline='')))


def csv_records_from_json(rows):
    import csv as _csv
    import io as _io
    if not rows:
        raise ValueError('CSV 需要至少一条数据。')
    fields = [field['key'] for field in PROFILE['columns']]
    stream = _io.StringIO(newline='')
    writer = _csv.DictWriter(stream, fieldnames=fields, extrasaction='ignore')
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key) for key in fields})
    return csv_records(stream.getvalue())


def has_strong_scenario_marker(headers):
    """Long-format scenario tables carry both an order and a scenario column."""
    from src.services.domain import _ORDER_KEYS, _SCENARIO_KEYS, _find_header
    return bool(_find_header(headers, _ORDER_KEYS) and _find_header(headers, _SCENARIO_KEYS))


def looks_like_scenario_headers(headers):
    if has_strong_scenario_marker(headers):
        return True
    from src.services.domain import _ORDER_KEYS, _find_header, _norm_header
    if not _find_header(headers, _ORDER_KEYS):
        return False
    known = ORDER_HEADER_SET | {'情景', 'scenario', 'scenario_id', 'duration', 'duration_min'}
    extras = [h for h in headers if _norm_header(h) not in {_norm_header(k) for k in known}]
    return bool(extras)


@router.post('/scenario-datasets', status_code=201)
def import_scenario_dataset(body: ScenarioImport, account=Depends(analyst)):
    if body.csv:
        payload = read_csv_dicts(body.csv)
    elif body.rows is not None:
        payload = body.rows
    else:
        raise ValueError('请提供逐情景工时 CSV 或 rows 数据。')
    # Optional synchronous exact-cover check against a registered order revision/day.
    reference_id = body.dataset_id
    extra = getattr(body, 'model_extra', None) or {}
    if reference_id is None:
        for key in ('order_dataset_id', 'orders_dataset_id'):
            if extra.get(key):
                reference_id = int(extra[key])
                break
    if reference_id and body.work_date:
        from datetime import date as _date
        from src.services.domain import _orders
        order_dataset = require_row(datasets, reference_id, '工单数据版本')
        orders = _orders(order_dataset['records'])
        wanted = [o.code for o in orders if o.recorded is None
                  and _date.fromordinal(o.day).isoformat() == str(body.work_date)[:10]]
        normalize_scenarios(payload, wanted)
    records = scenario_records_from_payload(payload)
    key, created = save_revision(body.name, records, account['username'], 'scenarios')
    return {'id': key, 'created': created, 'rows': len(records), 'kind': 'scenarios'}


@router.get('/scenario-datasets')
def list_scenario_datasets(account=Depends(signed_in)):
    return revisions('scenarios')



@router.post('/login')
def login(body: Login):
    return authenticate(body.username, body.password)


@router.get('/me')
def current(account=Depends(signed_in)):
    return dict(account)


@router.get('/profile')
def profile():
    return PROFILE


def download_csv(data, name):
    return Response(data, media_type='text/csv; charset=utf-8',
                    headers={'content-disposition': "attachment; filename*=UTF-8''" + quote(name)})


@router.get('/example.csv')
def example(account=Depends(signed_in)):
    return download_csv(write_csv(sample_rows(), [c['key'] for c in PROFILE['columns']], False), PROFILE['filename'])


@router.get('/datasets')
def list_datasets(kind: str | None = None, account=Depends(signed_in)):
    if kind not in (None, '', 'orders', 'scenarios'):
        kind = None
    return revisions(kind or None)


@router.get('/datasets/scenarios')
def list_scenario_datasets_nested(account=Depends(signed_in)):
    return revisions('scenarios')


@router.get('/datasets/{key}')
def get_dataset(key: int, account=Depends(signed_in)):
    dataset = require_row(datasets, key, '数据版本')
    records = dataset.pop('records')
    return {**dataset, 'count': len(records), 'records': records[:100]}


@router.post('/datasets', status_code=201)
def import_data(body: Import, account=Depends(analyst)):
    rows, kind = parse_import(body)
    key, created = save_revision(body.name, rows, account['username'], kind)
    return {'id': key, 'created': created, 'rows': len(rows), 'kind': kind}


@router.post('/datasets/scenarios', status_code=201)
def import_scenario_dataset_alias(body: ScenarioImport, account=Depends(analyst)):
    return import_scenario_dataset(body, account)


@router.get('/datasets/scenarios/list')
def list_scenario_dataset_alias(account=Depends(signed_in)):
    return revisions('scenarios')


@router.post('/scenarios', status_code=201)
def create_scenarios(body: ScenarioImport, account=Depends(analyst)):
    return import_scenario_dataset(body, account)


@router.get('/scenarios')
def get_scenarios(account=Depends(signed_in)):
    return revisions('scenarios')


class TeamPreview(BaseModel):
    dataset_id: int | None = Field(default=None, gt=0)
    scenario_dataset_id: int | None = Field(default=None, gt=0)
    work_date: str | None = None
    target_pct: float | None = None
    options: dict = Field(default_factory=dict)
    scenarios: object | None = None
    orders: list | None = None
    model_config = ConfigDict(extra='allow')


def _dataset_records(key):
    dataset = require_row(datasets, key, '数据版本')
    return dataset['records']


@router.post('/team-plan/preview')
@router.post('/team-plan')
@router.post('/assignments')
@router.post('/team-assignment')
@router.post('/runs/team-plan')
@router.post('/plan/teams')
def team_plan_preview(body: TeamPreview, account=Depends(signed_in)):
    from src.services.domain import _orders
    target = body.target_pct if body.target_pct is not None else body.options.get('target_pct', 80)
    work_date = body.work_date or body.options.get('work_date')
    if body.orders is not None:
        orders = body.orders
    elif body.dataset_id:
        orders = _orders(_dataset_records(body.dataset_id))
        pending = [o for o in orders if o.recorded is None]
        if work_date:
            pending = [o for o in pending if __import__('datetime').date.fromordinal(o.day).isoformat() == str(work_date)[:10]]
        orders = pending
    else:
        raise HTTPException(422, '请提供工单数据版本或 orders。')
    scenario_payload = body.scenarios
    if scenario_payload is None and body.scenario_dataset_id:
        scenario_payload = _dataset_records(body.scenario_dataset_id)
    try:
        return team_assignment(orders, scenario_payload, target_pct=target, work_date=work_date)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error


@router.get('/runs')
def list_runs(account=Depends(signed_in)):
    return jobs()


@router.post('/runs', status_code=202)
def queue(body: Run, account=Depends(analyst)):
    return add_run(body, account['username'])


@router.get('/runs/{key}')
def result(key: int, account=Depends(signed_in)):
    return require_row(runs, key, '分析任务')


@router.post('/runs/{key}/review')
def review(key: int, body: Review, account=Depends(reviewer)):
    return review_run(key, body, account['username'])


@router.post('/runs/{key}/schedules', status_code=201)
def schedule(key: int, body: Delivery, account=Depends(reviewer)):
    return schedule_report(key, body, account['username'])


@router.get('/delivery')
def delivery(account=Depends(signed_in)):
    return delivery_status()


@router.delete('/schedules/{key}')
def stop_schedule(key: int, account=Depends(reviewer)):
    return cancel_schedule(key, account['username'])


@router.get('/audit')
def activity(account=Depends(signed_in)):
    return recent_activity()


@router.get('/runs/{key}/report.csv')
def report(key: int, account=Depends(signed_in)):
    run = require_row(runs, key, '分析任务')
    if run['decision'] != 'approved' and account['role'] == Role.viewer.value:
        raise HTTPException(403, '只读账号可导出已批准的报告。')
    if not run['result']:
        raise HTTPException(409, '分析结果尚不可导出。')
    path = ARTIFACTS / f"run-{key}-{run['started'].hex()}.csv"
    data = path.read_bytes() if path.exists() else write_csv(run['result']['rows'], run['result'].get('columns'))
    return download_csv(data, f"{PROFILE['title']}-{key}.csv")
