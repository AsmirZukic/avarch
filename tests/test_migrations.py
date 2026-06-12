from pathlib import Path

from sqlalchemy import inspect

from avarch.db import create_db_engine
from avarch.db_migrations import upgrade_database


def test_alembic_upgrade_creates_tables(tmp_path: Path) -> None:
    db_path = tmp_path / "avarch.db"
    database_url = f"sqlite:///{db_path}"

    upgrade_database(database_url)

    engine = create_db_engine(database_url)
    inspector = inspect(engine)

    assert "appmeta" in inspector.get_table_names()
    assert "mediafile" in inspector.get_table_names()
