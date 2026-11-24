import time

from fastapi import HTTPException
from sqlalchemy import and_, insert, or_, select, update

from src.constants.enums import State
from src.models.database import add_event, engine, fetch_one
from src.models.tables import datasets, deliveries, runs, schedules
from src.services.data import parse_options, save_revision

SCENARIO_ID_ALIASES = (
    'scenario_dataset_id', 'scenario_id', 'scenarios_id', 'scenario_revision_id',
    'scenarios_dataset_id', 'hours_dataset_id', 'workhour_dataset_id',
    'work_hours_dataset_id', 'second_dataset_id', 'extra_dataset_id',
)
DATE_ALIASES = ('work_date', 'job_date', 'operation_date', 'day', 'date', '作业日')
PRIMARY_ID_ALIASES = (
    'dataset_id', 'order_dataset_id', 'orders_dataset_id', 'primary_dataset_id',
    'ledger_dataset_id', 'data_id', 'revision_id',
)


def _extra(body):
    return getattr(body, 'model_extra', None) or {}


def _resolve_primary_id(body, raw_options):
    for field in PRIMARY_ID_ALIASES:
        value = getattr(body, field, None)
        if value is None:
            value = _extra(body).get(field)
        if value is None and isinstance(raw_options, dict):
            value = raw_options.get(field)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                raise HTTPException(422, '工单数据版本编号无效。')
    return None


def _resolve_scenario_id(body, options):
    for field in SCENARIO_ID_ALIASES:
        value = getattr(body, field, None)
        if value is None:
            value = _extra(body).get(field)
        if value is None and isinstance(options, dict):
            value = options.get(field)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                raise HTTPException(422, '情景工时数据版本编号无效。')
    return None


def _resolve_work_date(body, raw_options):
    for field in DATE_ALIASES:
        value = getattr(body, field, None)
        if value is None:
            value = _extra(body).get(field)
        if value is None and isinstance(raw_options, dict):
            value = raw_options.get(field)
        if value not in (None, ''):
            return str(value)[:10]
    return None


def add_run(body, actor):
    raw_options = dict(body.options or {})
    options = parse_options(raw_options)
    primary_id = _resolve_primary_id(body, raw_options)
    if primary_id is None:
        raise HTTPException(422, '请选择工单数据版本 dataset_id。')
    work_date = _resolve_work_date(body, raw_options)
    if work_date:
        options['work_date'] = work_date

    inline_scenarios = getattr(body, 'scenarios', None)
    scenario_id = _resolve_scenario_id(body, raw_options)
    if inline_scenarios is not None and scenario_id is None:
        from src.services.domain import normalize_scenarios, to_long_scenario_records
        records = to_long_scenario_records(normalize_scenarios(inline_scenarios))
        scenario_id, _created = save_revision('逐情景工时（随分析登记）', records, actor, 'scenarios')
    now = time.time()
    with engine.begin() as connection:
        if fetch_one(connection, datasets, primary_id) is None:
            raise HTTPException(404, '数据版本不存在。')
        if scenario_id is not None and fetch_one(connection, datasets, scenario_id) is None:
            raise HTTPException(404, '逐情景工时数据版本不存在，请先补传。')
        key = connection.execute(insert(runs).values(
            dataset_id=primary_id, scenario_dataset_id=scenario_id, work_date=work_date,
            options=options, state=State.queued.value, owner=actor, created=now)).inserted_primary_key[0]
        add_event(connection, actor, 'queue', key, now)
    return {'id': key, 'state': State.queued.value, 'dataset_id': primary_id,
            'scenario_dataset_id': scenario_id, 'work_date': work_date}


def scenario_inputs_for_run(key):
    """Reload the pinned second revision for a run (used after interruption/recovery)."""
    with engine.connect() as connection:
        run = fetch_one(connection, runs, key)
        if run is None or not run['scenario_dataset_id']:
            return None
        dataset = fetch_one(connection, datasets, run['scenario_dataset_id'])
        return dataset['records'] if dataset is not None else None


def claim_run():
    now = time.time()
    claimable = or_(runs.c.state == State.queued.value, and_(runs.c.state == State.running.value, runs.c.started < now - 120))
    with engine.begin() as connection:
        run = connection.execute(select(runs).where(claimable).order_by(runs.c.id).limit(1)).mappings().one_or_none()
        if run is None:
            return None
        key = run['id']
        claim = connection.execute(update(runs).where(runs.c.id == key, claimable)
                                   .values(state=State.running.value, started=now))
        if claim.rowcount != 1:
            return None
        data = fetch_one(connection, datasets, run['dataset_id'])
        return key, now, data['records'], run['options']


def finish_run(key, claim, result, error):
    now = time.time()
    state = State.failed.value if error else State.completed.value
    with engine.begin() as connection:
        saved = connection.execute(update(runs).where(runs.c.id == key, runs.c.started == claim,
                                   runs.c.state == State.running.value).values(state=state, result=result,
                                                  error=error, finished=now))
        if saved.rowcount:
            add_event(connection, 'worker', state, key, now)


def review_run(key, body, actor):
    now = time.time()
    note = body.note.strip()
    if len(note) < 2:
        raise ValueError('请输入至少两个字符的复核意见。')
    with engine.begin() as connection:
        run = fetch_one(connection, runs, key)
        if run is None:
            raise HTTPException(404, '分析任务不存在。')
        if run['state'] != State.completed.value:
            raise HTTPException(409, '分析完成后才能复核。')
        if body.decision == 'approved' and run['result'].get('can_publish') is False:
            raise HTTPException(409, '结果未达到本项目的发布门槛。')
        updated = connection.execute(update(runs).where(runs.c.id == key, runs.c.decision.is_(None))
                                     .values(decision=body.decision, reviewer=actor, review_note=note, reviewed=now))
        if updated.rowcount != 1:
            raise HTTPException(409, '已完成复核。请重新运行分析以提交新的复核。')
        add_event(connection, actor, body.decision, key, now)
    return {'decision': body.decision}


def schedule_report(key, body, actor):
    now = time.time()
    recipient = body.recipient.strip()
    if len(recipient) < 2:
        raise ValueError('请填写站内收件组。')
    if body.scheduled_at and body.scheduled_at.utcoffset() is None:
        raise ValueError('投递时间须含时区。')
    timestamp = body.scheduled_at.timestamp() if body.scheduled_at else now
    if body.scheduled_at and timestamp < now - 60:
        raise ValueError('投递时间已过，请调整时间。')
    with engine.begin() as connection:
        run = fetch_one(connection, runs, key)
        if run is None or run['decision'] != 'approved':
            raise HTTPException(409, '仅已批准的结果可分发。')
        required_recipient = run['result'].get('dispatch_recipient')
        if required_recipient and recipient != required_recipient:
            raise HTTPException(422, f'该报告应投递给{required_recipient}。')
        schedule_id = connection.execute(insert(schedules).values(run_id=key, recipient=recipient, cadence=body.cadence,
                                      next_at=timestamp, active=1, owner=actor)).inserted_primary_key[0]
        add_event(connection, actor, 'schedule', schedule_id, now)
    return {'id': schedule_id, 'next_at': timestamp}


def cancel_schedule(key, actor):
    with engine.begin() as connection:
        updated = connection.execute(update(schedules).where(schedules.c.id == key, schedules.c.active == 1)
                                     .values(active=0))
        if not updated.rowcount:
            raise HTTPException(409, '该计划已停止或已完成。')
        add_event(connection, actor, 'cancel', key, time.time())
    return {'id': key, 'active': False}


def publish_due():
    now = time.time()
    with engine.begin() as connection:
        pending = connection.execute(select(schedules).where(schedules.c.active == 1, schedules.c.next_at <= now)
                                     .order_by(schedules.c.next_at, schedules.c.id).limit(20)).mappings().all()
        for item in pending:
            previous = item['next_at']
            daily = item['cadence'] == 'daily'
            future = previous + (int((now - previous) / 86400) + 1) * 86400 if daily else previous
            changed = connection.execute(update(schedules).where(schedules.c.id == item['id'], schedules.c.active == 1,
                              schedules.c.next_at == previous).values(active=int(daily), next_at=future))
            if changed.rowcount:
                delivery_id = connection.execute(insert(deliveries).values(schedule_id=item['id'], run_id=item['run_id'],
                                               recipient=item['recipient'], scheduled=previous, delivered=now)).inserted_primary_key[0]
                add_event(connection, 'worker', 'delivered', delivery_id, now)
