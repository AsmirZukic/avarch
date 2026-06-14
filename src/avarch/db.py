from __future__ import annotations

from typing import Any

from sqlalchemy import Engine, event
from sqlmodel import SQLModel, create_engine


def create_db_engine(database_url: str) -> Engine:
    engine = create_engine(database_url)
    if database_url.startswith("sqlite"):
        _enable_sqlite_foreign_keys(engine)
    return engine


def _enable_sqlite_foreign_keys(engine: Engine) -> None:
    event.listen(engine, "connect", _set_sqlite_pragma)


def _set_sqlite_pragma(dbapi_connection: Any, _connection_record: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def create_db_schema(engine: Engine) -> None:
    SQLModel.metadata.create_all(engine)
