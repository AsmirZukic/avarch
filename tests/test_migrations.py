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
        "fs_fingerprint",
        "last_seen_at",
        "status",
        "latest_probe_id",
    } <= columns
    assert "ix_mediafile_fs_fingerprint" in indexes
    assert "ix_mediafile_latest_probe_id" in indexes


def test_upgrade_creates_probe_result_table(tmp_path: Path) -> None:
    db_path = tmp_path / "avarch.db"
    database_url = f"sqlite:///{db_path}"

    upgrade_database(database_url)

    engine = create_db_engine(database_url)
    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("proberesult")}
    indexes = {index["name"] for index in inspector.get_indexes("proberesult")}

    assert {
        "id",
        "media_file_id",
        "ffprobe_json",
        "normalized_json",
        "probe_hash",
        "source_fs_fingerprint",
        "created_at",
    } <= columns
    assert "ix_proberesult_media_file_id" in indexes
    assert "ix_proberesult_probe_hash" in indexes


def test_probe_result_foreign_key_targets_media_file(tmp_path: Path) -> None:
    db_path = tmp_path / "avarch.db"
    database_url = f"sqlite:///{db_path}"

    upgrade_database(database_url)

    engine = create_db_engine(database_url)
    foreign_keys = inspect(engine).get_foreign_keys("proberesult")

    assert foreign_keys[0]["referred_table"] == "mediafile"
    assert foreign_keys[0]["referred_columns"] == ["id"]


def test_latest_probe_foreign_key_targets_probe_result(tmp_path: Path) -> None:
    db_path = tmp_path / "avarch.db"
    database_url = f"sqlite:///{db_path}"

    upgrade_database(database_url)

    engine = create_db_engine(database_url)
    foreign_keys = inspect(engine).get_foreign_keys("mediafile")

    assert any(
        foreign_key["referred_table"] == "proberesult"
        and foreign_key["constrained_columns"] == ["latest_probe_id"]
        and foreign_key["referred_columns"] == ["id"]
        for foreign_key in foreign_keys
    )


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
    assert row["fs_fingerprint"].startswith("legacy:")
    assert row["last_seen_at"] is not None
    assert row["status"] == "present"
    assert row["latest_probe_id"] is None


def test_migration_preserves_existing_fingerprint_values(tmp_path: Path) -> None:
    db_path = tmp_path / "avarch.db"
    database_url = f"sqlite:///{db_path}"
    config = _alembic_config(database_url)
    command.upgrade(config, "41f0d8b4b5e1")

    engine = create_db_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO mediafile (
                    path,
                    size_bytes,
                    mtime_ns,
                    discovered_at,
                    device_id,
                    inode,
                    content_key,
                    last_seen_at,
                    status
                )
                VALUES (
                    :path,
                    :size_bytes,
                    :mtime_ns,
                    :discovered_at,
                    :device_id,
                    :inode,
                    :content_key,
                    :last_seen_at,
                    :status
                )
                """
            ),
            {
                "path": "/media/movie.mkv",
                "size_bytes": 123,
                "mtime_ns": 456,
                "discovered_at": "2026-06-14 00:00:00",
                "device_id": 1,
                "inode": 2,
                "content_key": "key",
                "last_seen_at": "2026-06-14 00:00:00",
                "status": "present",
            },
        )

    command.upgrade(config, "head")

    with engine.begin() as connection:
        media_file = connection.execute(sa.text("SELECT * FROM mediafile")).mappings().one()
        connection.execute(
            sa.text(
                """
                INSERT INTO proberesult (
                    media_file_id,
                    ffprobe_json,
                    normalized_json,
                    probe_hash,
                    created_at
                )
                VALUES (:media_file_id, '{}', '{}', 'hash', :created_at)
                """
            ),
            {
                "media_file_id": media_file["id"],
                "created_at": "2026-06-14 00:00:00",
            },
        )
        probe_count = connection.execute(sa.text("SELECT count(*) FROM proberesult")).scalar_one()

    assert media_file["path"] == "/media/movie.mkv"
    assert media_file["fs_fingerprint"] == "key"
    assert probe_count == 1


def test_legacy_probe_rows_have_no_source_fingerprint(tmp_path: Path) -> None:
    db_path = tmp_path / "avarch.db"
    database_url = f"sqlite:///{db_path}"
    config = _alembic_config(database_url)
    command.upgrade(config, "41f0d8b4b5e1")

    engine = create_db_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO mediafile (
                    path,
                    size_bytes,
                    mtime_ns,
                    discovered_at,
                    device_id,
                    inode,
                    content_key,
                    last_seen_at,
                    status
                )
                VALUES (
                    :path,
                    :size_bytes,
                    :mtime_ns,
                    :discovered_at,
                    :device_id,
                    :inode,
                    :content_key,
                    :last_seen_at,
                    :status
                )
                """
            ),
            {
                "path": "/media/movie.mkv",
                "size_bytes": 123,
                "mtime_ns": 456,
                "discovered_at": "2026-06-14 00:00:00",
                "device_id": 1,
                "inode": 2,
                "content_key": "key",
                "last_seen_at": "2026-06-14 00:00:00",
                "status": "present",
            },
        )
        media_file = connection.execute(sa.text("SELECT * FROM mediafile")).mappings().one()
        connection.execute(
            sa.text(
                """
                INSERT INTO proberesult (
                    media_file_id,
                    ffprobe_json,
                    normalized_json,
                    probe_hash,
                    created_at
                )
                VALUES (:media_file_id, '{}', '{}', 'hash', :created_at)
                """
            ),
            {
                "media_file_id": media_file["id"],
                "created_at": "2026-06-14 00:00:00",
            },
        )

    command.upgrade(config, "head")

    with engine.connect() as connection:
        probe_result = connection.execute(sa.text("SELECT * FROM proberesult")).mappings().one()
        media_file = connection.execute(sa.text("SELECT * FROM mediafile")).mappings().one()

    assert probe_result["source_fs_fingerprint"] is None
    assert media_file["latest_probe_id"] is None


def test_migration_upgrades_legacy_content_key_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "avarch.db"
    database_url = f"sqlite:///{db_path}"
    config = _alembic_config(database_url)
    command.upgrade(config, "41f0d8b4b5e1")

    upgrade_database(database_url)

    engine = create_db_engine(database_url)
    inspector = inspect(engine)
    media_columns = {column["name"] for column in inspector.get_columns("mediafile")}
    probe_columns = {column["name"] for column in inspector.get_columns("proberesult")}
    indexes = {index["name"] for index in inspector.get_indexes("mediafile")}

    assert "content_key" not in media_columns
    assert "fs_fingerprint" in media_columns
    assert "latest_probe_id" in media_columns
    assert "source_fs_fingerprint" in probe_columns
    assert "ix_mediafile_fs_fingerprint" in indexes
    assert "ix_mediafile_latest_probe_id" in indexes


def test_migration_recovers_after_partial_source_fingerprint_add(tmp_path: Path) -> None:
    db_path = tmp_path / "avarch.db"
    database_url = f"sqlite:///{db_path}"
    config = _alembic_config(database_url)
    command.upgrade(config, "41f0d8b4b5e1")
    engine = create_db_engine(database_url)
    with engine.begin() as connection:
        connection.execute(
            sa.text("ALTER TABLE proberesult ADD COLUMN source_fs_fingerprint VARCHAR")
        )

    upgrade_database(database_url)

    inspector = inspect(engine)
    media_columns = {column["name"] for column in inspector.get_columns("mediafile")}
    probe_columns = [
        column["name"] for column in inspector.get_columns("proberesult")
    ]
    with engine.connect() as connection:
        revision = connection.execute(
            sa.text("SELECT version_num FROM alembic_version")
        ).scalar_one()

    assert "content_key" not in media_columns
    assert "fs_fingerprint" in media_columns
    assert "latest_probe_id" in media_columns
    assert probe_columns.count("source_fs_fingerprint") == 1
    assert revision == "7d56d2f55f31"


def _alembic_config(database_url: str) -> Config:
    project_root = Path(__file__).resolve().parents[1]
    config = Config(str(project_root / "alembic.ini"))
    config.attributes["configure_logger"] = False
    config.set_main_option("script_location", str(project_root / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config
