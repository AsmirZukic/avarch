from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
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


def test_fresh_database_upgrades_to_inventory_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "avarch.db"
    database_url = f"sqlite:///{db_path}"

    upgrade_database(database_url)

    engine = create_db_engine(database_url)
    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("mediafile")}
    indexes = {index["name"] for index in inspector.get_indexes("mediafile")}

    assert {
        "device_id",
        "inode",
        "content_key",
        "last_seen_at",
        "status",
    } <= columns
    assert "ix_mediafile_content_key" in indexes


def test_milestone_one_database_upgrades_without_losing_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "avarch.db"
    database_url = f"sqlite:///{db_path}"
    config = _alembic_config(database_url)
    command.upgrade(config, "9aaf75d07ce6")

    engine = create_db_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO mediafile (path, size_bytes, mtime_ns, discovered_at)
                VALUES (:path, :size_bytes, :mtime_ns, :discovered_at)
                """
            ),
            {
                "path": "/media/legacy.mkv",
                "size_bytes": 123,
                "mtime_ns": 456,
                "discovered_at": "2026-06-14 00:00:00",
            },
        )

    command.upgrade(config, "head")

    with engine.connect() as connection:
        row = connection.execute(sa.text("SELECT * FROM mediafile")).mappings().one()

    assert row["path"] == "/media/legacy.mkv"
    assert row["device_id"] == 0
    assert row["inode"] == 0
    assert row["content_key"].startswith("legacy:")
    assert row["last_seen_at"] is not None
    assert row["status"] == "present"


def _alembic_config(database_url: str) -> Config:
    project_root = Path(__file__).resolve().parents[1]
    config = Config(str(project_root / "alembic.ini"))
    config.attributes["configure_logger"] = False
    config.set_main_option("script_location", str(project_root / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config
