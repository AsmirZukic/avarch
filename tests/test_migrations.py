from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

from avarch.contracts import ALEMBIC_BASELINE_REVISION, ALEMBIC_HEAD_REVISION
from avarch.db import create_db_engine
from avarch.db_migrations import migration_project_root, upgrade_database


def test_repository_contains_migration_revisions() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    revisions = sorted((repo_root / "migrations" / "versions").glob("*.py"))

    assert [revision.name for revision in revisions] == [
        "0001_initial_schema.py",
        "0002_media_plan.py",
        "0003_job_outcome_reason.py",
    ]


def test_migration_project_root_can_be_configured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("AVARCH_MIGRATIONS_ROOT", str(tmp_path))

    assert migration_project_root() == tmp_path


def test_initial_revision_has_no_parent() -> None:
    script = ScriptDirectory.from_config(_alembic_config("sqlite:///:memory:"))
    revision = script.get_revision(ALEMBIC_BASELINE_REVISION)

    assert revision is not None
    assert revision.down_revision is None


def test_media_plan_revision_depends_on_initial_revision() -> None:
    script = ScriptDirectory.from_config(_alembic_config("sqlite:///:memory:"))
    revision = script.get_revision("0002_media_plan")

    assert revision is not None
    assert revision.down_revision == ALEMBIC_BASELINE_REVISION


def test_job_outcome_reason_revision_depends_on_media_plan() -> None:
    script = ScriptDirectory.from_config(_alembic_config("sqlite:///:memory:"))
    revision = script.get_revision(ALEMBIC_HEAD_REVISION)

    assert revision is not None
    assert revision.down_revision == "0002_media_plan"


def test_fresh_upgrade_creates_all_tables(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'avarch.db'}"

    upgrade_database(database_url)

    tables = set(inspect(create_db_engine(database_url)).get_table_names())

    assert {
        "alembic_version",
        "appmeta",
        "mediafile",
        "proberesult",
        "job",
        "jobattempt",
        "mediaplan",
        "promotionrecord",
        "schedulerstate",
        "validationresult",
    } <= tables


def test_initial_revision_creates_indexes(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'avarch.db'}"

    upgrade_database(database_url)

    inspector = inspect(create_db_engine(database_url))
    media_indexes = {index["name"] for index in inspector.get_indexes("mediafile")}
    probe_indexes = {index["name"] for index in inspector.get_indexes("proberesult")}
    job_indexes = {index["name"] for index in inspector.get_indexes("job")}
    validation_indexes = {index["name"] for index in inspector.get_indexes("validationresult")}
    promotion_indexes = {index["name"] for index in inspector.get_indexes("promotionrecord")}
    media_plan_indexes = {index["name"] for index in inspector.get_indexes("mediaplan")}

    assert "ix_mediafile_fs_fingerprint" in media_indexes
    assert "ix_mediafile_latest_probe_id" in media_indexes
    assert "ix_proberesult_media_file_id" in probe_indexes
    assert "ix_proberesult_probe_hash" in probe_indexes
    assert "ix_job_queue_key" in job_indexes
    assert "ix_job_plan_hash" in job_indexes
    assert "ix_validationresult_attempt_id" in validation_indexes
    assert "ix_promotionrecord_operation_id" in promotion_indexes
    assert "ix_promotionrecord_attempt_id" in promotion_indexes
    assert "ix_mediaplan_plan_hash" in media_plan_indexes
    assert "ix_mediaplan_media_file_id" in media_plan_indexes


def test_initial_revision_creates_foreign_keys(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'avarch.db'}"

    upgrade_database(database_url)

    inspector = inspect(create_db_engine(database_url))

    assert _has_foreign_key(
        inspector,
        table="mediafile",
        columns=["latest_probe_id"],
        referred_table="proberesult",
    )
    assert _has_foreign_key(
        inspector,
        table="proberesult",
        columns=["media_file_id"],
        referred_table="mediafile",
    )
    assert _has_foreign_key(
        inspector,
        table="mediaplan",
        columns=["media_file_id"],
        referred_table="mediafile",
    )
    assert _has_foreign_key(
        inspector,
        table="mediaplan",
        columns=["probe_result_id"],
        referred_table="proberesult",
    )
    assert _has_foreign_key(
        inspector,
        table="job",
        columns=["latest_validation_id"],
        referred_table="validationresult",
    )
    assert _has_foreign_key(
        inspector,
        table="validationresult",
        columns=["job_id"],
        referred_table="job",
    )
    assert _has_foreign_key(
        inspector,
        table="job",
        columns=["latest_promotion_id"],
        referred_table="promotionrecord",
    )
    assert _has_foreign_key(
        inspector,
        table="promotionrecord",
        columns=["validation_result_id"],
        referred_table="validationresult",
    )


def test_probe_fingerprint_is_nonnullable(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'avarch.db'}"

    upgrade_database(database_url)

    columns = {
        column["name"]: column
        for column in inspect(create_db_engine(database_url)).get_columns("proberesult")
    }

    assert columns["source_fs_fingerprint"]["nullable"] is False


def test_upgrade_passes_foreign_key_check_and_cycles_can_be_updated(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'avarch.db'}"

    upgrade_database(database_url)

    engine = create_db_engine(database_url)
    now = datetime(2026, 6, 15, tzinfo=UTC)
    with engine.begin() as connection:
        media_id = _insert(connection, "mediafile", _media_values(now))
        probe_id = _insert(connection, "proberesult", _probe_values(media_id, now))
        connection.execute(
            sa.text("UPDATE mediafile SET latest_probe_id = :probe_id WHERE id = :media_id"),
            {"probe_id": probe_id, "media_id": media_id},
        )
        job_id = _insert(connection, "job", _job_values(media_id, probe_id, now))
        attempt_id = _insert(connection, "jobattempt", _attempt_values(job_id, now))
        validation_id = _insert(
            connection,
            "validationresult",
            _validation_values(job_id, attempt_id, now),
        )
        promotion_id = _insert(
            connection,
            "promotionrecord",
            _promotion_values(job_id, attempt_id, validation_id, now),
        )
        connection.execute(
            sa.text("UPDATE job SET latest_validation_id = :validation_id WHERE id = :job_id"),
            {"validation_id": validation_id, "job_id": job_id},
        )
        connection.execute(
            sa.text("UPDATE job SET latest_promotion_id = :promotion_id WHERE id = :job_id"),
            {"promotion_id": promotion_id, "job_id": job_id},
        )

        violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()

    assert violations == []


def test_downgrade_to_base_and_reupgrade_succeed(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'avarch.db'}"
    config = _alembic_config(database_url)

    command.upgrade(config, "head")
    command.downgrade(config, "base")
    command.upgrade(config, "head")

    tables = set(inspect(create_db_engine(database_url)).get_table_names())
    assert "mediafile" in tables


def test_upgrade_from_initial_revision_adds_media_plan(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'avarch.db'}"
    config = _alembic_config(database_url)

    command.upgrade(config, ALEMBIC_BASELINE_REVISION)
    tables_before = set(inspect(create_db_engine(database_url)).get_table_names())
    assert "mediaplan" not in tables_before

    upgrade_database(database_url)

    inspector = inspect(create_db_engine(database_url))
    assert "mediaplan" in set(inspector.get_table_names())


def test_upgrade_adds_job_outcome_reason(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'avarch.db'}"

    upgrade_database(database_url)

    columns = {
        column["name"] for column in inspect(create_db_engine(database_url)).get_columns("job")
    }
    assert "outcome_reason" in columns


def test_alembic_has_one_head() -> None:
    script = ScriptDirectory.from_config(_alembic_config("sqlite:///:memory:"))

    assert script.get_heads() == [ALEMBIC_HEAD_REVISION]


def _has_foreign_key(
    inspector: sa.Inspector,
    *,
    table: str,
    columns: list[str],
    referred_table: str,
) -> bool:
    return any(
        foreign_key["constrained_columns"] == columns
        and foreign_key["referred_table"] == referred_table
        for foreign_key in inspector.get_foreign_keys(table)
    )


def _insert(
    connection: sa.Connection,
    table: str,
    values: dict[str, object],
) -> int:
    columns = ", ".join(values)
    placeholders = ", ".join(f":{key}" for key in values)
    result = connection.execute(
        sa.text(f"INSERT INTO {table} ({columns}) VALUES ({placeholders})"),
        values,
    )
    return int(result.lastrowid)


def _media_values(now: datetime) -> dict[str, object]:
    return {
        "path": "/media/movie.mkv",
        "size_bytes": 1,
        "mtime_ns": 2,
        "device_id": 3,
        "inode": 4,
        "fs_fingerprint": "source-fs",
        "discovered_at": now,
        "last_seen_at": now,
        "status": "present",
    }


def _probe_values(media_id: int, now: datetime) -> dict[str, object]:
    return {
        "media_file_id": media_id,
        "ffprobe_json": "{}",
        "normalized_json": "{}",
        "probe_hash": "probe-hash",
        "source_fs_fingerprint": "source-fs",
        "created_at": now,
    }


def _job_values(media_id: int, probe_id: int, now: datetime) -> dict[str, object]:
    return {
        "media_file_id": media_id,
        "profile_name": "av1_1080p_sdr",
        "profile_hash": "profile-hash",
        "source_fs_fingerprint": "source-fs",
        "queue_key": "queue-key",
        "probe_result_id": probe_id,
        "probe_hash": "probe-hash",
        "status": "pending",
        "stage": "encode",
        "priority": 0,
        "attempts": 0,
        "created_at": now,
        "updated_at": now,
    }


def _attempt_values(job_id: int, now: datetime) -> dict[str, object]:
    return {
        "job_id": job_id,
        "attempt_number": 1,
        "stage": "encode",
        "resource_class": "cheap",
        "status": "completed",
        "runner_id": "runner",
        "started_at": now,
        "finished_at": now,
    }


def _validation_values(job_id: int, attempt_id: int, now: datetime) -> dict[str, object]:
    return {
        "job_id": job_id,
        "attempt_id": attempt_id,
        "plan_hash": "plan-hash",
        "policy_hash": "policy-hash",
        "output_path": "/output/movie.mkv",
        "passed": True,
        "details_json": "{}",
        "created_at": now,
    }


def _promotion_values(
    job_id: int,
    attempt_id: int,
    validation_id: int,
    now: datetime,
) -> dict[str, object]:
    return {
        "operation_id": "operation",
        "job_id": job_id,
        "attempt_id": attempt_id,
        "validation_result_id": validation_id,
        "mode": "keep-original",
        "status": "completed",
        "phase": "committed",
        "source_path": "/media/movie.mkv",
        "validated_output_path": "/work/movie.av1.mkv",
        "final_path": "/media/movie.av1.mkv",
        "staging_path": "/media/.movie.av1.mkv.avarch-promote-operation.tmp",
        "source_fingerprint_before": "source-fs",
        "source_stat_json": "{}",
        "validated_output_fingerprint": "output-fs",
        "journal_path": "/work/runtime/promotion-journal.json",
        "cleanup_completed": True,
        "created_at": now,
        "updated_at": now,
        "started_at": now,
    }


def _alembic_config(database_url: str) -> Config:
    project_root = Path(__file__).resolve().parents[1]
    config = Config(str(project_root / "alembic.ini"))
    config.attributes["configure_logger"] = False
    config.set_main_option("script_location", str(project_root / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config
