from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm.exc import StaleDataError
from sqlmodel import Session, select

from avarch.adapters.sqlite.db import create_db_engine, create_db_schema
from avarch.adapters.sqlite.job_transitions import (
    JobClaimError,
    JobTransitionError,
    claim_job_stage,
    complete_job_stage,
    fail_job_stage,
    interrupt_job_stage,
    queue_rejected_output_cleanup,
    transition_job,
)
from avarch.adapters.sqlite.models import Job, JobAttempt, MediaFile, MediaFileStatus
from avarch.domain.jobs import (
    AttemptStatus,
    JobOutcomeReason,
    JobStage,
    JobStatus,
    ResourceClass,
)


def test_claim_sets_job_running(tmp_path: Path) -> None:
    engine, job_id = _stored_job(tmp_path)
    now = datetime.now(UTC)

    with Session(engine) as session:
        claim_job_stage(session, job_id=job_id, runner_id="runner", now=now)
        session.commit()
        job = session.get(Job, job_id)

    assert job is not None
    assert job.status == JobStatus.ENCODING
    assert job.claimed_by == "runner"


def test_claim_creates_attempt(tmp_path: Path) -> None:
    engine, job_id = _stored_job(tmp_path)

    with Session(engine) as session:
        claim_job_stage(session, job_id=job_id, runner_id="runner", now=datetime.now(UTC))
        session.commit()
        attempt = session.exec(select(JobAttempt)).one()

    assert attempt.stage == JobStage.PROBE
    assert attempt.resource_class == ResourceClass.CHEAP


def test_claim_increments_attempt_count(tmp_path: Path) -> None:
    engine, job_id = _stored_job(tmp_path)

    with Session(engine) as session:
        claim_job_stage(session, job_id=job_id, runner_id="runner", now=datetime.now(UTC))
        session.commit()
        job = session.get(Job, job_id)

    assert job is not None
    assert job.attempts == 1


def test_claim_sets_first_started_at_only_once(tmp_path: Path) -> None:
    started = datetime.now(UTC) - timedelta(days=1)
    engine, job_id = _stored_job(tmp_path, started_at=started)

    with Session(engine) as session:
        claim_job_stage(session, job_id=job_id, runner_id="runner", now=datetime.now(UTC))
        session.commit()
        job = session.get(Job, job_id)

    assert job is not None
    assert job.started_at == started.replace(tzinfo=None)


def test_claim_rejects_nonpending_job(tmp_path: Path) -> None:
    engine, job_id = _stored_job(tmp_path, status=JobStatus.PROMOTED)

    with Session(engine) as session, pytest.raises(JobClaimError):
        claim_job_stage(session, job_id=job_id, runner_id="runner", now=datetime.now(UTC))


def test_completion_advances_stage(tmp_path: Path) -> None:
    engine, job_id = _stored_job(tmp_path)
    with Session(engine) as session:
        attempt = claim_job_stage(session, job_id=job_id, runner_id="runner", now=datetime.now(UTC))
        complete_job_stage(
            session,
            job_id=job_id,
            attempt_id=attempt.id or 0,
            next_stage=JobStage.PLAN,
            now=datetime.now(UTC),
        )
        session.commit()
        job = session.get(Job, job_id)

    assert job is not None
    assert job.status == JobStatus.QUEUED
    assert job.stage == JobStage.PLAN


def test_final_completion_marks_job_completed(tmp_path: Path) -> None:
    engine, job_id = _stored_job(tmp_path)
    with Session(engine) as session:
        attempt = claim_job_stage(session, job_id=job_id, runner_id="runner", now=datetime.now(UTC))
        complete_job_stage(
            session,
            job_id=job_id,
            attempt_id=attempt.id or 0,
            next_stage=None,
            now=datetime.now(UTC),
        )
        session.commit()
        job = session.get(Job, job_id)

    assert job is not None
    assert job.status == JobStatus.PROMOTED
    assert job.finished_at is not None


def test_failure_preserves_current_stage(tmp_path: Path) -> None:
    engine, job_id = _stored_job(tmp_path, stage=JobStage.ENCODE)
    with Session(engine) as session:
        attempt = claim_job_stage(session, job_id=job_id, runner_id="runner", now=datetime.now(UTC))
        fail_job_stage(
            session,
            job_id=job_id,
            attempt_id=attempt.id or 0,
            error=ValueError("broken"),
            exit_code=2,
            now=datetime.now(UTC),
        )
        session.commit()
        job = session.get(Job, job_id)

    assert job is not None
    assert job.status == JobStatus.FAILED
    assert job.stage == JobStage.ENCODE


def test_failure_records_error_type_and_message(tmp_path: Path) -> None:
    engine, job_id = _stored_job(tmp_path)
    with Session(engine) as session:
        attempt = claim_job_stage(session, job_id=job_id, runner_id="runner", now=datetime.now(UTC))
        fail_job_stage(
            session,
            job_id=job_id,
            attempt_id=attempt.id or 0,
            error=ValueError("broken"),
            exit_code=2,
            now=datetime.now(UTC),
        )
        session.commit()
        stored_attempt = session.get(JobAttempt, attempt.id)

    assert stored_attempt is not None
    assert stored_attempt.error_type == "ValueError"
    assert stored_attempt.error_message == "broken"


def test_interruption_returns_job_to_pending(tmp_path: Path) -> None:
    engine, job_id = _stored_job(tmp_path)
    with Session(engine) as session:
        attempt = claim_job_stage(session, job_id=job_id, runner_id="runner", now=datetime.now(UTC))
        interrupt_job_stage(
            session,
            job_id=job_id,
            attempt_id=attempt.id or 0,
            now=datetime.now(UTC),
        )
        session.commit()
        job = session.get(Job, job_id)

    assert job is not None
    assert job.status == JobStatus.QUEUED


def test_interruption_records_attempt_as_interrupted(tmp_path: Path) -> None:
    engine, job_id = _stored_job(tmp_path)
    with Session(engine) as session:
        attempt = claim_job_stage(session, job_id=job_id, runner_id="runner", now=datetime.now(UTC))
        interrupt_job_stage(
            session,
            job_id=job_id,
            attempt_id=attempt.id or 0,
            now=datetime.now(UTC),
        )
        session.commit()
        stored_attempt = session.get(JobAttempt, attempt.id)

    assert stored_attempt is not None
    assert stored_attempt.status == AttemptStatus.INTERRUPTED
    assert stored_attempt.exit_code == 130


def test_job_can_transition_from_encoded_to_validating(tmp_path: Path) -> None:
    engine, job_id = _stored_job(tmp_path, status=JobStatus.ENCODED, stage=JobStage.VALIDATE)
    now = datetime.now(UTC)

    with Session(engine) as session, session.begin():
        job = session.get(Job, job_id)
        assert job is not None
        transition_job(job, JobStatus.VALIDATING, now=now)

    with Session(engine) as session:
        job = session.get(Job, job_id)

    assert job is not None
    assert job.status == JobStatus.VALIDATING


def test_job_cannot_promote_from_encoding(tmp_path: Path) -> None:
    engine, job_id = _stored_job(tmp_path, status=JobStatus.ENCODING, stage=JobStage.ENCODE)

    with Session(engine) as session, session.begin(), pytest.raises(JobTransitionError):
        job = session.get(Job, job_id)
        assert job is not None
        transition_job(job, JobStatus.PROMOTING)


def test_job_records_outcome_reason(tmp_path: Path) -> None:
    engine, job_id = _stored_job(tmp_path, status=JobStatus.VALIDATING, stage=JobStage.VALIDATE)

    with Session(engine) as session, session.begin():
        job = session.get(Job, job_id)
        assert job is not None
        transition_job(
            job,
            JobStatus.VALIDATION_FAILED,
            reason=JobOutcomeReason.FAILED_VALIDATION,
        )

    with Session(engine) as session:
        job = session.get(Job, job_id)

    assert job is not None
    assert job.status == JobStatus.VALIDATION_FAILED
    assert job.outcome_reason == JobOutcomeReason.FAILED_VALIDATION


def test_invalid_transition_is_rejected(tmp_path: Path) -> None:
    engine, job_id = _stored_job(tmp_path, status=JobStatus.QUEUED)

    with Session(engine) as session, session.begin(), pytest.raises(JobTransitionError):
        job = session.get(Job, job_id)
        assert job is not None
        transition_job(job, JobStatus.PROMOTED)


def test_stale_job_write_is_rejected(tmp_path: Path) -> None:
    engine, job_id = _stored_job(tmp_path, status=JobStatus.QUEUED)
    first = Session(engine)
    second = Session(engine)
    try:
        first_job = first.get(Job, job_id)
        second_job = second.get(Job, job_id)
        assert first_job is not None
        assert second_job is not None

        transition_job(first_job, JobStatus.ENCODING, now=datetime.now(UTC))
        first.commit()

        transition_job(second_job, JobStatus.CANCELLED, now=datetime.now(UTC))
        with pytest.raises(StaleDataError):
            second.commit()
    finally:
        first.close()
        second.close()


def test_rejected_output_cleanup_handoff_queues_cleanup_stage(tmp_path: Path) -> None:
    engine, job_id = _stored_job(
        tmp_path,
        status=JobStatus.READY_TO_PROMOTE,
        stage=JobStage.PROMOTE,
    )
    now = datetime.now(UTC)

    with Session(engine) as session, session.begin():
        job = session.get(Job, job_id)
        assert job is not None
        queue_rejected_output_cleanup(job, now=now)

    with Session(engine) as session:
        job = session.get(Job, job_id)

    assert job is not None
    assert job.status == JobStatus.QUEUED
    assert job.stage == JobStage.CLEANUP


def _stored_job(
    tmp_path: Path,
    *,
    status: JobStatus = JobStatus.QUEUED,
    stage: JobStage = JobStage.PROBE,
    started_at: datetime | None = None,
) -> tuple[Engine, int]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'avarch.adapters.sqlite.db'}")
    create_db_schema(engine)
    now = datetime.now(UTC)
    with Session(engine) as session:
        media_file = MediaFile(
            path=str(tmp_path / "movie.mkv"),
            size_bytes=1,
            mtime_ns=2,
            device_id=3,
            inode=4,
            fs_fingerprint="fingerprint",
            discovered_at=now,
            last_seen_at=now,
            status=MediaFileStatus.PRESENT,
        )
        session.add(media_file)
        session.commit()
        session.refresh(media_file)
        job = Job(
            media_file_id=media_file.id or 0,
            profile_name="av1_1080p_sdr",
            profile_hash="profile-hash",
            source_fs_fingerprint="fingerprint",
            queue_key="queue-key",
            status=status,
            stage=stage,
            created_at=now,
            updated_at=now,
            started_at=started_at,
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        return engine, job.id or 0
