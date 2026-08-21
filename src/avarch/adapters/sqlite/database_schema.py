from __future__ import annotations

from pathlib import Path

from sqlalchemy.engine import make_url

from avarch.adapters.sqlite.db import create_db_engine, create_db_schema


def initialize_database_schema(database_url: str) -> None:
    _ensure_sqlite_parent(database_url)
    engine = create_db_engine(database_url)
    create_db_schema(engine)


def _ensure_sqlite_parent(database_url: str) -> None:
    url = make_url(database_url)
    database = url.database
    if url.drivername != "sqlite" or database is None or database in {"", ":memory:"}:
        return

    db_path = Path(database)
    if not db_path.is_absolute():
        db_path = Path.cwd() / db_path
    db_path.parent.mkdir(parents=True, exist_ok=True)
