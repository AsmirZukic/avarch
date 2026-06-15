from __future__ import annotations

from typing import Any

from sqlalchemy import Engine, event, inspect, text
from sqlalchemy.engine import make_url
from sqlmodel import SQLModel, create_engine

from avarch.contracts import ALEMBIC_BASELINE_REVISION

RESET_DATABASE_MESSAGE = """This database belongs to an unsupported development schema.

avarch no longer provides migration compatibility for earlier
development builds.

Delete the .avarch database and regenerate local state:

    rm -rf .avarch
    uv run avarch init --config ./avarch.toml
    uv run avarch db upgrade --config ./avarch.toml
    uv run avarch scan --config ./avarch.toml"""


class UnsupportedDatabaseSchemaError(RuntimeError):
    pass


def create_db_engine(database_url: str) -> Engine:
    connect_args: dict[str, Any] = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False

    engine = create_engine(database_url, connect_args=connect_args)
    if database_url.startswith("sqlite"):
        _configure_sqlite(engine, database_url)
    return engine


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
        cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


def _is_file_backed_sqlite(database_url: str) -> bool:
    url = make_url(database_url)
    database = url.database
    return url.drivername.startswith("sqlite") and database not in {None, "", ":memory:"}


def create_db_schema(engine: Engine) -> None:
    SQLModel.metadata.create_all(engine)


def verify_database_revision(
    engine: Engine,
    *,
    expected_revision: str = ALEMBIC_BASELINE_REVISION,
) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "alembic_version" not in tables:
        if tables:
            raise UnsupportedDatabaseSchemaError(RESET_DATABASE_MESSAGE)
        return

    with engine.connect() as connection:
        revisions = [
            row[0]
            for row in connection.execute(text("SELECT version_num FROM alembic_version")).all()
        ]

    if revisions == [expected_revision]:
        return

    raise UnsupportedDatabaseSchemaError(RESET_DATABASE_MESSAGE)
