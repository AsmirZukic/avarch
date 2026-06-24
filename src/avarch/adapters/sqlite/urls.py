from __future__ import annotations

from pathlib import Path

from sqlalchemy.engine import make_url

from avarch.config import AppConfig


def resolve_database_url(config: AppConfig, config_path: Path) -> str:
    url = make_url(config.database.url)
    database = url.database
    if url.drivername != "sqlite" or database is None or database in {"", ":memory:"}:
        return config.database.url

    db_path = Path(database)
    if not db_path.is_absolute():
        db_path = config_path.parent / db_path

    return url.set(database=str(db_path)).render_as_string(hide_password=False)
