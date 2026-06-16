from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from avarch.db import create_db_engine, create_db_schema
from avarch.models.db import (
    Job,
    JobAttempt,
    MediaFile,
    MediaFileStatus,
    PromotionRecord,
    SchedulerState,
    ValidationResult,
)
from avarch.models.promotion import PromotionMode, PromotionPhase, PromotionStatus
from avarch.models.scheduler import (
    AttemptStatus,
    JobStage,
    JobStatus,
    ResourceClass,
    SchedulerMode,
)


def test_insert_pending_probe_job(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)

    with Session(engine) as session:
        media_file = _media_file(now)
        session.add(media_file)
        session.commit()
        session.refresh(media_file)
        session.add(_job(media_file.id or 0, now))
        session.commit()

        stored = session.exec(select(Job)).one()

    assert stored.status == JobStatus.PENDING
    assert stored.stage == JobStage.PROBE


def test_job_queue_key_is_unique(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)

    with Session(engine) as session:
        media_file = _media_file(now)
        session.add(media_file)
        session.commit()
        session.refresh(media_file)
        session.add(_job(media_file.id or 0, now, queue_key="same"))
        session.add(_job(media_file.id or 0, now, queue_key="same"))

        with pytest.raises(IntegrityError):
            session.commit()


def test_non_null_plan_hash_is_unique(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)

    with Session(engine) as session:
        media_file = _media_file(now)
        session.add(media_file)
        session.commit()
        session.refresh(media_file)
        session.add(_job(media_file.id or 0, now, queue_key="one", plan_hash="plan"))
        session.add(_job(media_file.id or 0, now, queue_key="two", plan_hash="plan"))

        with pytest.raises(IntegrityError):
            session.commit()


def test_multiple_jobs_may_have_null_plan_hash(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)

    with Session(engine) as session:
        media_file = _media_file(now)
        session.add(media_file)
        session.commit()
        session.refresh(media_file)
        session.add(_job(media_file.id or 0, now, queue_key="one"))
        session.add(_job(media_file.id or 0, now, queue_key="two"))
        session.commit()

        rows = session.exec(select(Job)).all()

    assert len(rows) == 2


def test_insert_job_attempt(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)

    with Session(engine) as session:
        media_file = _media_file(now)
        session.add(media_file)
        session.commit()
        session.refresh(media_file)
        job = _job(media_file.id or 0, now)
        session.add(job)
        session.commit()
        session.refresh(job)
        session.add(
            JobAttempt(
                job_id=job.id or 0,
                attempt_number=1,
                stage=JobStage.PROBE,
                resource_class=ResourceClass.CHEAP,
                status=AttemptStatus.RUNNING,
                runner_id="runner",
                started_at=now,
            )
        )
        session.commit()

        stored = session.exec(select(JobAttempt)).one()

    assert stored.status == AttemptStatus.RUNNING


def test_attempt_number_is_unique_per_job(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)

    with Session(engine) as session:
        media_file = _media_file(now)
        session.add(media_file)
        session.commit()
        session.refresh(media_file)
        job = _job(media_file.id or 0, now)
        session.add(job)
        session.commit()
        session.refresh(job)
        for _ in range(2):
            session.add(
                JobAttempt(
                    job_id=job.id or 0,
                    attempt_number=1,
                    stage=JobStage.PROBE,
                    resource_class=ResourceClass.CHEAP,
                    status=AttemptStatus.RUNNING,
                    runner_id="runner",
                    started_at=now,
                )
            )

        with pytest.raises(IntegrityError):
            session.commit()


def test_scheduler_state_singleton_can_be_created(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)

    with Session(engine) as session:
        session.add(SchedulerState(id=1, mode=SchedulerMode.RUNNING, updated_at=now))
        session.commit()

        stored = session.get(SchedulerState, 1)

    assert stored is not None
    assert stored.mode == SchedulerMode.RUNNING


def test_job_enums_round_trip_through_sqlite(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)

    with Session(engine) as session:
        media_file = _media_file(now)
        session.add(media_file)
        session.commit()
        session.refresh(media_file)
        session.add(_job(media_file.id or 0, now, stage=JobStage.PLAN))
        session.commit()

        stored = session.exec(select(Job)).one()

    assert stored.stage == JobStage.PLAN
    assert stored.status == JobStatus.PENDING


def test_validated_promote_job_enums_round_trip(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)

    with Session(engine) as session:
        media_file = _media_file(now)
        session.add(media_file)
        session.commit()
        session.refresh(media_file)
        session.add(
            _job(
                media_file.id or 0,
                now,
                status=JobStatus.VALIDATED,
                stage=JobStage.PROMOTE,
            )
        )
        session.commit()

        stored = session.exec(select(Job)).one()

    assert stored.status == JobStatus.VALIDATED
    assert stored.stage == JobStage.PROMOTE


def test_insert_prepared_promotion_record(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)

    with Session(engine) as session:
        media_file = _media_file(now)
        session.add(media_file)
        session.commit()
        session.refresh(media_file)
        job = _job(media_file.id or 0, now, stage=JobStage.PROMOTE)
        session.add(job)
        session.commit()
        session.refresh(job)
        validation_attempt = JobAttempt(
            job_id=job.id or 0,
            attempt_number=1,
            stage=JobStage.VALIDATE,
            resource_class=ResourceClass.CHEAP,
            status=AttemptStatus.COMPLETED,
            runner_id="runner",
            started_at=now,
            finished_at=now,
        )
        session.add(validation_attempt)
        session.commit()
        session.refresh(validation_attempt)
        validation = ValidationResult(
            job_id=job.id or 0,
            attempt_id=validation_attempt.id or 0,
            plan_hash="plan-hash",
            policy_hash="policy-hash",
            output_path="/work/movie.av1.mkv",
            output_fs_fingerprint="output-fs",
            passed=True,
            details_json="{}",
            created_at=now,
        )
        session.add(validation)
        session.commit()
        session.refresh(validation)
        promotion_attempt = JobAttempt(
            job_id=job.id or 0,
            attempt_number=2,
            stage=JobStage.PROMOTE,
            resource_class=ResourceClass.FILE_OP,
            status=AttemptStatus.RUNNING,
            runner_id="runner",
            started_at=now,
        )
        session.add(promotion_attempt)
        session.commit()
        session.refresh(promotion_attempt)
        session.add(
            PromotionRecord(
                operation_id="operation",
                job_id=job.id or 0,
                attempt_id=promotion_attempt.id or 0,
                validation_result_id=validation.id or 0,
                mode=PromotionMode.KEEP_ORIGINAL,
                status=PromotionStatus.RUNNING,
                phase=PromotionPhase.PREPARED,
                source_path="/media/movie.mkv",
                validated_output_path="/work/movie.av1.mkv",
                final_path="/media/movie.av1.mkv",
                staging_path="/media/.movie.av1.mkv.avarch-promote-operation.tmp",
                backup_path=None,
                source_fingerprint_before="source-fs",
                source_stat_json="{}",
                validated_output_fingerprint="output-fs",
                validated_output_digest=None,
                staging_digest=None,
                final_fingerprint=None,
                final_digest=None,
                journal_path="/work/runtime/promotion-journal.json",
                created_at=now,
                updated_at=now,
                started_at=now,
            )
        )
        session.commit()

        stored = session.exec(select(PromotionRecord)).one()

    assert stored.mode == PromotionMode.KEEP_ORIGINAL
    assert stored.phase == PromotionPhase.PREPARED


def _engine(tmp_path: Path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'avarch.db'}")
    create_db_schema(engine)
    return engine


def _media_file(now: datetime) -> MediaFile:
    return MediaFile(
        path=f"/media/movie-{now.timestamp()}.mkv",
        size_bytes=1,
        mtime_ns=2,
        device_id=3,
        inode=4,
        fs_fingerprint="fingerprint",
        discovered_at=now,
        last_seen_at=now,
        status=MediaFileStatus.PRESENT,
    )


def _job(
    media_file_id: int,
    now: datetime,
    *,
    queue_key: str = "queue-key",
    plan_hash: str | None = None,
    status: JobStatus = JobStatus.PENDING,
    stage: JobStage = JobStage.PROBE,
) -> Job:
    return Job(
        media_file_id=media_file_id,
        profile_name="av1_1080p_sdr",
        profile_hash="profile-hash",
        source_fs_fingerprint="fingerprint",
        queue_key=queue_key,
        plan_hash=plan_hash,
        status=status,
        stage=stage,
        created_at=now,
        updated_at=now,
    )
