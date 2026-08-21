from __future__ import annotations

from importlib import import_module
from typing import Any

from sqlalchemy import event, inspect
from sqlalchemy.engine import Engine, make_url
from sqlmodel import SQLModel, create_engine


def create_db_engine(database_url: str) -> Engine:
    connect_args: dict[str, Any] = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False

    engine = create_engine(database_url, connect_args=connect_args)
    if database_url.startswith("sqlite"):
        _configure_sqlite(engine, database_url)
    return engine


def database_table_names(database_url: str) -> set[str]:
    engine = create_db_engine(database_url)
    with engine.connect():
        return set(inspect(engine).get_table_names())


def _configure_sqlite(engine: Engine, database_url: str) -> None:
    file_backed = _is_file_backed_sqlite(database_url)
    event.listen(engine, "connect", _sqlite_connect_listener(file_backed=file_backed))


def _sqlite_connect_listener(*, file_backed: bool) -> Any:
    def listener(dbapi_connection: Any, connection_record: Any) -> None:
        _set_sqlite_pragmas(
            dbapi_connection,
            connection_record,
            file_backed=file_backed,
        )

    return listener


def _set_sqlite_pragmas(
    dbapi_connection: Any,
    _connection_record: Any,
    *,
    file_backed: bool,
) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA busy_timeout=5000")
    if file_backed:
        cursor.execute("PRAGMA journal_mode=DELETE")
        cursor.execute("PRAGMA synchronous=FULL")
    cursor.close()


def _is_file_backed_sqlite(database_url: str) -> bool:
    url = make_url(database_url)
    database = url.database
    return url.drivername.startswith("sqlite") and database not in {None, "", ":memory:"}


def create_db_schema(engine: Engine) -> None:
    import_module("avarch.adapters.sqlite.models")
    SQLModel.metadata.create_all(engine)
