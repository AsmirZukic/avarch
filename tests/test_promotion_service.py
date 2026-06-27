from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlmodel import Session

from avarch.adapters.filesystem.scanner import create_file_snapshot
from avarch.adapters.promotion_service import (
    claim_promotion,
    mark_promotion_failed_or_validated,
    promote_job,
)
from avarch.adapters.sqlite.db import create_db_engine, create_db_schema
from avarch.adapters.sqlite.job_transitions import JobTransitionError, transition_job
from avarch.adapters.sqlite.models import (
    Job,
    JobAttempt,
    MediaFile,
    MediaFileStatus,
    PromotionRecord,
    ValidationResult,
)
from avarch.config import AppConfig, DatabaseSettings
from avarch.domain.jobs import (
    AttemptStatus,
    JobOutcomeReason,
    JobStage,
    JobStatus,
    ResourceClass,
)
from avarch.models.promotion import PromotionMode, PromotionPhase, PromotionStatus
from avarch.serialization import canonical_json
from tests.test_plan_models import sample_plan


def test_promotion_replaces_original_with_encoded_file(tmp_path: Path) -> None:
    config, job_id, source, encoded = _ready_job(tmp_path, output_bytes=b"encoded")

    result = asyncio.run(promote_job(job_id, config=config, mode=PromotionMode.REPLACE_ATOMIC))

    assert result.promoted is True
    assert result.status == PromotionStatus.COMPLETED
    assert source.read_bytes() == b"encoded"
    assert not encoded.exists()


def test_promotion_never_deletes_original_first(tmp_path: Path) -> None:
    config, job_id, source, _encoded = _ready_job(tmp_path, output_bytes=b"encoded")

    asyncio.run(promote_job(job_id, config=config, mode=PromotionMode.REPLACE_ATOMIC))

    assert source.exists()
    assert source.read_bytes() == b"encoded"
    assert not source.with_name(source.name + ".avarch-original").exists()


def test_promotion_fails_if_original_changed(tmp_path: Path) -> None:
    config, job_id, source, _encoded = _ready_job(tmp_path, output_bytes=b"encoded")
    source.write_bytes(b"changed original")

    result = asyncio.run(promote_job(job_id, config=config, mode=PromotionMode.REPLACE_ATOMIC))

    assert result.promoted is False
    assert "Source fingerprint" in (result.error_message or "")
    assert source.read_bytes() == b"changed original"


def test_promotion_fails_if_encoded_missing(tmp_path: Path) -> None:
    config, job_id, source, encoded = _ready_job(tmp_path, output_bytes=b"encoded")
    encoded.unlink()

    result = asyncio.run(promote_job(job_id, config=config, mode=PromotionMode.REPLACE_ATOMIC))

    assert result.promoted is False
    assert "Validated output" in (result.error_message or "")
    assert source.read_bytes() == b"original"


def test_promotion_is_retry_safe_after_partial_move(tmp_path: Path) -> None:
    config, job_id, source, encoded = _ready_job(tmp_path, output_bytes=b"encoded")
    backup = source.with_name(source.name + ".avarch-original")
    backup.write_bytes(source.read_bytes())

    result = asyncio.run(promote_job(job_id, config=config, mode=PromotionMode.REPLACE_ATOMIC))

    assert result.promoted is False
    assert source.read_bytes() == b"original"
    assert encoded.read_bytes() == b"encoded"


def test_two_jobs_cannot_promote_same_target(tmp_path: Path) -> None:
    config, job_id, source, _encoded = _ready_job(tmp_path, output_bytes=b"encoded")
    engine = create_db_engine(config.database.url)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        _insert_active_target_lock(session, target=source, now=now)
        try:
            claim_promotion(
                session,
                job_id=job_id,
                mode=PromotionMode.REPLACE_ATOMIC,
                owner_token="owner",
                now=now,
            )
        except Exception as exc:
            error = exc
        else:
            error = None

    assert error is not None
    assert "already locked" in str(error)


def test_failed_promotion_releases_lock(tmp_path: Path) -> None:
    config, job_id, _source, _encoded = _ready_job(tmp_path, output_bytes=b"encoded")
    engine = create_db_engine(config.database.url)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        record = claim_promotion(
            session,
            job_id=job_id,
            mode=PromotionMode.REPLACE_ATOMIC,
            owner_token="owner",
            now=now,
        )
        promotion_id = record.id or 0
        mark_promotion_failed_or_validated(
            session,
            promotion_id=promotion_id,
            error=RuntimeError("promotion failed"),
            now=now,
        )

    with Session(engine) as session:
        record = session.get(PromotionRecord, promotion_id)

    assert record is not None
    assert record.owner_token is None
    assert record.lease_expires_at is None
    assert record.status == PromotionStatus.FAILED


def test_promoted_job_keeps_terminal_state(tmp_path: Path) -> None:
    config, job_id, _source, _encoded = _ready_job(tmp_path, output_bytes=b"encoded")
    asyncio.run(promote_job(job_id, config=config, mode=PromotionMode.REPLACE_ATOMIC))
    engine = create_db_engine(config.database.url)

    with Session(engine) as session, session.begin():
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == JobStatus.PROMOTED
        assert job.outcome_reason == JobOutcomeReason.SUCCESS
        try:
            transition_job(job, JobStatus.READY_TO_PROMOTE)
        except JobTransitionError as exc:
            error = exc
        else:
            error = None

    assert error is not None


def _ready_job(
    tmp_path: Path,
    *,
    output_bytes: bytes,
) -> tuple[AppConfig, int, Path, Path]:
    database_path = tmp_path / "avarch.adapters.sqlite.db"
    config = AppConfig(database=DatabaseSettings(url=f"sqlite:///{database_path}"))
    engine = create_db_engine(config.database.url)
    create_db_schema(engine)
    now = datetime.now(UTC)
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"original")
    source_snapshot = create_file_snapshot(source)
    work_dir = tmp_path / "work"
    runtime_dir = work_dir / "runtime"
    artifact_dir = tmp_path / "plans"
    encoded = work_dir / "movie.av1.mkv"
    encoded.parent.mkdir(parents=True, exist_ok=True)
    encoded.write_bytes(output_bytes)
    base = sample_plan()
    plan = base.model_copy(
        update={
            "input_path": source,
            "output_path": encoded,
            "temp_dir": work_dir,
            "source_fs_fingerprint": source_snapshot.fs_fingerprint,
            "av1an": base.av1an.model_copy(
                update={
                    "video_output_path": work_dir / "video-only.mkv",
                    "temp_dir": work_dir / "av1an",
                    "working_directory": work_dir,
                }
            ),
            "mux": base.mux.model_copy(
                update={
                    "video_input_path": work_dir / "video-only.mkv",
                    "source_input_path": source,
                    "output_path": encoded,
                }
            ),
            "runtime": base.runtime.model_copy(
                update={
                    "runtime_dir": runtime_dir,
                    "av1an_stdout_log": runtime_dir / "av1an.stdout.log",
                    "av1an_stderr_log": runtime_dir / "av1an.stderr.log",
                    "mux_stdout_log": runtime_dir / "mux.stdout.log",
                    "mux_stderr_log": runtime_dir / "mux.stderr.log",
                    "av1an_stage_marker": runtime_dir / "av1an-stage.json",
                    "encode_result": runtime_dir / "encode-result.json",
                    "validation_report": runtime_dir / "validation-report.json",
                    "validation_decode_stdout_log": runtime_dir / "validation.decode.stdout.log",
                    "validation_decode_stderr_log": runtime_dir / "validation.decode.stderr.log",
                }
            ),
            "artifacts": base.artifacts.model_copy(
                update={
                    "artifact_dir": artifact_dir,
                    "plan_json": artifact_dir / "plan.json",
                    "vapoursynth_script": artifact_dir / "movie.vpy",
                    "av1an_command_json": artifact_dir / "av1an.command.json",
                    "validation_policy_json": artifact_dir / "validation-policy.json",
                }
            ),
        }
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "plan.json").write_text(canonical_json(plan) + "\n", encoding="utf-8")

    with Session(engine) as session, session.begin():
        media_file = MediaFile(
            path=str(source),
            size_bytes=source_snapshot.size_bytes,
            mtime_ns=source_snapshot.mtime_ns,
            device_id=source_snapshot.device_id,
            inode=source_snapshot.inode,
            fs_fingerprint=source_snapshot.fs_fingerprint,
            discovered_at=now,
            last_seen_at=now,
            status=MediaFileStatus.PRESENT,
        )
        session.add(media_file)
        session.flush()
        job = Job(
            media_file_id=media_file.id or 0,
            profile_name="av1_1080p_sdr",
            profile_hash="profile-hash",
            source_fs_fingerprint=source_snapshot.fs_fingerprint,
            queue_key=f"queue-{now.timestamp()}",
            plan_hash=plan.plan_hash,
            plan_path=str(plan.artifacts.plan_json),
            output_path=str(encoded),
            status=JobStatus.READY_TO_PROMOTE,
            stage=JobStage.PROMOTE,
            attempts=1,
            created_at=now,
            updated_at=now,
        )
        session.add(job)
        session.flush()
        attempt = JobAttempt(
            job_id=job.id or 0,
            attempt_number=1,
            stage=JobStage.VALIDATE,
            resource_class=ResourceClass.CHEAP,
            status=AttemptStatus.COMPLETED,
            runner_id="runner",
            started_at=now,
            finished_at=now,
        )
        session.add(attempt)
        session.flush()
        validation = ValidationResult(
            job_id=job.id or 0,
            attempt_id=attempt.id or 0,
            plan_hash=plan.plan_hash,
            policy_hash=plan.validation.policy_hash,
            output_path=str(encoded),
            output_fs_fingerprint=create_file_snapshot(encoded).fs_fingerprint,
            passed=True,
            details_json="{}",
            created_at=now,
        )
        session.add(validation)
        session.flush()
        job.latest_validation_id = validation.id
        session.add(job)
        job_id = job.id or 0

    return config, job_id, source, encoded


def _insert_active_target_lock(session: Session, *, target: Path, now: datetime) -> None:
    job = Job(
        media_file_id=1,
        profile_name="av1_1080p_sdr",
        profile_hash="profile-hash",
        source_fs_fingerprint="other-fingerprint",
        queue_key=f"lock-{now.timestamp()}",
        plan_hash="lock-plan",
        status=JobStatus.PROMOTING,
        stage=JobStage.PROMOTE,
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    session.flush()
    attempt = JobAttempt(
        job_id=job.id or 0,
        attempt_number=1,
        stage=JobStage.PROMOTE,
        resource_class=ResourceClass.FILE_OP,
        status=AttemptStatus.RUNNING,
        runner_id="other-owner",
        started_at=now,
    )
    session.add(attempt)
    session.flush()
    validation = ValidationResult(
        job_id=job.id or 0,
        attempt_id=attempt.id or 0,
        plan_hash="lock-plan",
        policy_hash="policy-hash",
        output_path=str(target.with_name("other-output.mkv")),
        output_fs_fingerprint="output-fingerprint",
        passed=True,
        details_json="{}",
        created_at=now,
    )
    session.add(validation)
    session.flush()
    session.add(
        PromotionRecord(
            operation_id="locked-target",
            job_id=job.id or 0,
            attempt_id=attempt.id or 0,
            validation_result_id=validation.id or 0,
            mode=PromotionMode.REPLACE_ATOMIC,
            status=PromotionStatus.RUNNING,
            phase=PromotionPhase.PREPARED,
            source_path=str(target.with_name("other-source.mkv")),
            validated_output_path=str(target.with_name("other-output.mkv")),
            final_path=str(target),
            promotion_target_path=str(target),
            staging_path=str(target.with_name(".other.tmp")),
            backup_path=str(target.with_name("other.backup")),
            source_fingerprint_before="source-fingerprint",
            source_stat_json="{}",
            validated_output_fingerprint="output-fingerprint",
            validated_output_digest=None,
            staging_digest=None,
            final_fingerprint=None,
            final_digest=None,
            journal_path=str(target.with_name("journal.json")),
            owner_token="other-owner",
            heartbeat_at=now,
            lease_expires_at=now + timedelta(seconds=30),
            created_at=now,
            updated_at=now,
            started_at=now,
        )
    )
