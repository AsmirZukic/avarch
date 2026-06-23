from __future__ import annotations

import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.util.exc import CommandError
from sqlalchemy.engine import make_url

from avarch.db import (
    RESET_DATABASE_MESSAGE,
    UnsupportedDatabaseSchemaError,
    create_db_engine,
    verify_database_revision,
)


def upgrade_database(database_url: str) -> None:
    _ensure_sqlite_parent(database_url)
    engine = create_db_engine(database_url)
    verify_database_revision(engine)
    try:
        command.upgrade(_alembic_config(database_url), "head")
    except CommandError as exc:
        raise UnsupportedDatabaseSchemaError(RESET_DATABASE_MESSAGE) from exc


def get_current_revision(database_url: str) -> str | None:
    engine = create_db_engine(database_url)
    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        revision = context.get_current_revision()

    return revision


def _alembic_config(database_url: str) -> Config:
    project_root = migration_project_root()
    config = Config(str(project_root / "alembic.ini"))
    config.attributes["configure_logger"] = False
    config.set_main_option("script_location", str(project_root / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def migration_project_root() -> Path:
    configured_root = os.environ.get("AVARCH_MIGRATIONS_ROOT")
    if configured_root:
        return Path(configured_root)

    return Path(__file__).resolve().parents[2]


def _ensure_sqlite_parent(database_url: str) -> None:
    url = make_url(database_url)
    database = url.database
    if url.drivername != "sqlite" or database is None or database in {"", ":memory:"}:
        return

    db_path = Path(database)
    if not db_path.is_absolute():
        db_path = Path.cwd() / db_path

    db_path.parent.mkdir(parents=True, exist_ok=True)
