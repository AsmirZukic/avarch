from datetime import UTC, datetime
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy import inspect

from avarch.adapters.sqlite.database_schema import initialize_database_schema
from avarch.adapters.sqlite.db import create_db_engine


def test_fresh_schema_initialization_creates_all_tables(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'avarch.db'}"

    initialize_database_schema(database_url)

    tables = set(inspect(create_db_engine(database_url)).get_table_names())

    assert {
        "mediafile",
        "proberesult",
        "job",
        "jobattempt",
        "scheduler_session",
        "job_attempt_progress",
        "mediaplan",
        "promotionrecord",
        "schedulerstate",
        "validationresult",
    } <= tables


def test_schema_initialization_is_idempotent(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'avarch.db'}"

    initialize_database_schema(database_url)
    initialize_database_schema(database_url)

    assert "mediafile" in inspect(create_db_engine(database_url)).get_table_names()


def test_current_schema_creates_indexes(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'avarch.db'}"

    initialize_database_schema(database_url)

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
    assert "ix_promotionrecord_promotion_target_path" in promotion_indexes
    assert "ix_mediaplan_plan_hash" in media_plan_indexes
    assert "ix_mediaplan_media_file_id" in media_plan_indexes


def test_current_schema_creates_foreign_keys(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'avarch.db'}"

    initialize_database_schema(database_url)

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
    assert _has_foreign_key(
        inspector,
        table="job_attempt_progress",
        columns=["attempt_id"],
        referred_table="jobattempt",
    )
    assert _has_foreign_key(
        inspector,
        table="jobattempt",
        columns=["scheduler_session_id"],
        referred_table="scheduler_session",
    )
    assert _has_foreign_key(
        inspector,
        table="jobevent",
        columns=["attempt_id"],
        referred_table="jobattempt",
    )
    assert _has_foreign_key(
        inspector,
        table="jobevent",
        columns=["scheduler_session_id"],
        referred_table="scheduler_session",
    )


def test_job_attempt_progress_columns_match_contract(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'avarch.db'}"

    initialize_database_schema(database_url)

    inspector = inspect(create_db_engine(database_url))
    columns = {column["name"]: column for column in inspector.get_columns("job_attempt_progress")}
    primary_key = inspector.get_pk_constraint("job_attempt_progress")

    assert primary_key["constrained_columns"] == ["attempt_id"]
    assert columns["attempt_id"]["nullable"] is False
    assert columns["phase"]["nullable"] is False
    assert columns["current_value"]["nullable"] is True
    assert columns["total_value"]["nullable"] is True
    assert columns["unit"]["nullable"] is True
    assert columns["rate_per_second"]["nullable"] is True
    assert columns["speed_ratio"]["nullable"] is True
    assert columns["chunks_current"]["nullable"] is True
    assert columns["chunks_total"]["nullable"] is True
    assert columns["bitrate_kbps"]["nullable"] is True
    assert columns["estimated_output_bytes"]["nullable"] is True
    assert columns["written_output_bytes"]["nullable"] is True
    assert columns["source"]["nullable"] is False
    assert columns["message"]["nullable"] is True
    assert columns["phase_started_at"]["nullable"] is False
    assert columns["observed_at"]["nullable"] is False
    assert columns["heartbeat_at"]["nullable"] is False
    assert columns["advanced_at"]["nullable"] is True
    assert columns["created_at"]["nullable"] is False
    assert columns["updated_at"]["nullable"] is False


def test_scheduler_session_columns_match_contract(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'avarch.db'}"

    initialize_database_schema(database_url)

    inspector = inspect(create_db_engine(database_url))
    columns = {column["name"]: column for column in inspector.get_columns("scheduler_session")}
    primary_key = inspector.get_pk_constraint("scheduler_session")

    assert primary_key["constrained_columns"] == ["id"]
    assert columns["owner_id"]["nullable"] is False
    assert columns["workspace_id"]["nullable"] is False
    assert columns["pid"]["nullable"] is True
    assert columns["host"]["nullable"] is False
    assert columns["started_at"]["nullable"] is False
    assert columns["ended_at"]["nullable"] is True
    assert columns["end_reason"]["nullable"] is True


def test_lifecycle_metadata_columns_are_nullable(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'avarch.db'}"

    initialize_database_schema(database_url)

    inspector = inspect(create_db_engine(database_url))
    attempt_columns = {column["name"]: column for column in inspector.get_columns("jobattempt")}
    event_columns = {column["name"]: column for column in inspector.get_columns("jobevent")}

    assert attempt_columns["scheduler_session_id"]["nullable"] is True
    assert event_columns["attempt_id"]["nullable"] is True
    assert event_columns["scheduler_session_id"]["nullable"] is True
    assert event_columns["stage"]["nullable"] is True
    assert event_columns["details_json"]["nullable"] is True
    assert event_columns["dedupe_key"]["nullable"] is True


def test_scheduler_runtime_capacity_columns_are_nullable(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'avarch.db'}"

    initialize_database_schema(database_url)

    columns = {
        column["name"]: column
        for column in inspect(create_db_engine(database_url)).get_columns("schedulerstate")
    }

    for column_name in (
        "capacity_cheap_workers",
        "capacity_cheap_active",
        "capacity_av1an_jobs",
        "capacity_av1an_active",
        "capacity_file_ops",
        "capacity_file_ops_active",
        "capacity_observed_at",
    ):
        assert columns[column_name]["nullable"] is True


def test_probe_fingerprint_is_nonnullable(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'avarch.db'}"

    initialize_database_schema(database_url)

    columns = {
        column["name"]: column
        for column in inspect(create_db_engine(database_url)).get_columns("proberesult")
    }

    assert columns["source_fs_fingerprint"]["nullable"] is False


def test_schema_passes_foreign_key_check_and_cycles_can_be_updated(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'avarch.db'}"

    initialize_database_schema(database_url)

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


def test_current_schema_has_job_outcome_reason(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'avarch.db'}"

    initialize_database_schema(database_url)

    columns = {
        column["name"] for column in inspect(create_db_engine(database_url)).get_columns("job")
    }
    assert "outcome_reason" in columns


def test_current_schema_has_promotion_target_lock_key(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'avarch.db'}"

    initialize_database_schema(database_url)

    columns = {
        column["name"]
        for column in inspect(create_db_engine(database_url)).get_columns("promotionrecord")
    }
    assert "promotion_target_path" in columns


def test_current_schema_has_job_state_version(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'avarch.db'}"

    initialize_database_schema(database_url)

    columns = {
        column["name"]: column
        for column in inspect(create_db_engine(database_url)).get_columns("job")
    }
    assert columns["state_version"]["nullable"] is False


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
        "status": "present",
    }


def _probe_values(media_id: int, now: datetime) -> dict[str, object]:
    return {
        "media_file_id": media_id,
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
        "state_version": 1,
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
