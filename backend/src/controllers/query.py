from fastapi import HTTPException
from sqlalchemy import select

from src.models.database import engine, fetch_one
from src.models.tables import audit, datasets, deliveries, runs, schedules


def list_rows(table, fields=None, limit=100):
    statement = select(*fields) if fields else select(table)
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(statement.order_by(table.c.id.desc()).limit(limit)).mappings()]


def revisions(kind=None):
    columns = [field for field in datasets.c if field.name != 'records']
    statement = select(*columns)
    if kind:
        statement = statement.where(datasets.c.kind == kind)
    return list_rows_from_statement(statement)


def list_rows_from_statement(statement, limit=100):
    with engine.connect() as connection:
        return [dict(row) for row in connection.execute(
            statement.order_by(datasets.c.id.desc()).limit(limit)).mappings()]


def require_row(table, key, label):
    with engine.connect() as connection:
        row = fetch_one(connection, table, key)
    if row is None:
        raise HTTPException(404, label + '不存在。')
    return dict(row)


def jobs():
    return list_rows(runs, [field for field in runs.c if field.name != 'result'])


def delivery_status():
    return {'schedules': list_rows(schedules), 'deliveries': list_rows(deliveries)}


def recent_activity():
    return list_rows(audit, limit=20)
