from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlmodel import Session, col, select

from avarch.adapters.sqlite.db import create_db_engine, create_db_schema
from avarch.adapters.sqlite.job_control import (
    JobControlError,
    cancel_job,
    hold_job,
    release_job,
    update_job_priority,
)
from avarch.adapters.sqlite.job_transitions import (
    claim_job_stage,
    complete_job_stage,
    fail_job_stage,
    interrupt_job_stage,
    recover_abandoned_jobs,
)
from avarch.adapters.sqlite.models import (
    Job,
    JobAttempt,
    JobEvent,
    MediaFile,
    MediaFileStatus,
)
from avarch.domain.jobs import (
    AttemptStatus,
    JobEventType,
    JobStage,
    JobStatus,
)

_STAGE_EVENT_TYPES = {
    JobEventType.STAGE_STARTED,
    JobEventType.STAGE_COMPLETED,
    JobEventType.STAGE_FAILED,
    JobEventType.STAGE_CANCELLED,
}


@pytest.mark.parametrize(
    "status,stage",
    [
        (JobStatus.QUEUED, JobStage.PROBE),
        (JobStatus.HELD, JobStage.PLAN),
        (JobStatus.FAILED, JobStage.ENCODE),
        (JobStatus.READY_TO_PROMOTE, JobStage.PROMOTE),
    ],
)
def test_cancel_nonrunning_eligible_job_is_immediate(
    tmp_path: Path,
    status: JobStatus,
    stage: JobStage,
) -> None:
    engine, job_id = _stored_job(tmp_path, status=status, stage=stage)
    now = datetime.now(UTC)

    with Session(engine) as session, session.begin():
        cancel_job(session, job_id=job_id, actor="test", reason="wrong profile", now=now)

    job, events = _job_and_events(engine, job_id)
    assert job.status == JobStatus.CANCELLED
    assert job.cancel_requested_at == now.replace(tzinfo=None)
    assert job.cancel_requested_by == "test"
    assert job.cancel_reason == "wrong profile"
    assert job.canceled_at == now.replace(tzinfo=None)
    assert job.finished_at == now.replace(tzinfo=None)
    assert [event.event_type for event in events] == [JobEventType.CANCELED]


@pytest.mark.parametrize("status", [JobStatus.PROMOTED, JobStatus.SKIPPED])
def test_cancel_rejects_terminal_history_states(tmp_path: Path, status: JobStatus) -> None:
    engine, job_id = _stored_job(tmp_path, status=status)

    with Session(engine) as session, session.begin(), pytest.raises(JobControlError):
        cancel_job(session, job_id=job_id, actor="test", now=datetime.now(UTC))


def test_cancel_running_scheduler_job_records_request(tmp_path: Path) -> None:
    engine, job_id = _stored_job(tmp_path, status=JobStatus.QUEUED)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        claim_job_stage(session, job_id=job_id, runner_id="runner", now=now)
    with Session(engine) as session, session.begin():
        cancel_job(session, job_id=job_id, actor="test", reason="stop this", now=now)

    job, events = _job_and_events(engine, job_id)
    assert job.status == JobStatus.ENCODING
    assert job.cancel_requested_at == now.replace(tzinfo=None)
    assert job.cancel_requested_by == "test"
    assert job.canceled_at is None
    assert [event.event_type for event in events] == [JobEventType.CANCEL_REQUESTED]


def test_cancel_running_promotion_is_rejected(tmp_path: Path) -> None:
    engine, job_id = _stored_job(
        tmp_path,
        status=JobStatus.ENCODING,
        stage=JobStage.PROMOTE,
    )

    with Session(engine) as session, session.begin(), pytest.raises(JobControlError):
        cancel_job(session, job_id=job_id, actor="test", now=datetime.now(UTC))


def test_cancel_is_idempotent_for_already_canceled_job(tmp_path: Path) -> None:
    engine, job_id = _stored_job(tmp_path, status=JobStatus.CANCELLED)

    with Session(engine) as session, session.begin():
        cancel_job(session, job_id=job_id, actor="test", now=datetime.now(UTC))

    _job, events = _job_and_events(engine, job_id)
    assert events == []


def test_hold_pending_job_is_immediate(tmp_path: Path) -> None:
    engine, job_id = _stored_job(tmp_path)
    now = datetime.now(UTC)

    with Session(engine) as session, session.begin():
        hold_job(session, job_id=job_id, actor="test", reason="inspect", now=now)

    job, events = _job_and_events(engine, job_id)
    assert job.status == JobStatus.HELD
    assert job.hold_requested_at == now.replace(tzinfo=None)
    assert job.hold_requested_by == "test"
    assert job.hold_reason == "inspect"
    assert job.held_at == now.replace(tzinfo=None)
    assert [event.event_type for event in events] == [JobEventType.HELD]


def test_hold_running_job_records_request_without_interrupting(tmp_path: Path) -> None:
    engine, job_id = _stored_job(tmp_path)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        claim_job_stage(session, job_id=job_id, runner_id="runner", now=now)
    with Session(engine) as session, session.begin():
        hold_job(session, job_id=job_id, actor="test", reason="later", now=now)

    job, events = _job_and_events(engine, job_id)
    assert job.status == JobStatus.ENCODING
    assert job.hold_requested_at == now.replace(tzinfo=None)
    assert job.held_at is None
    assert [event.event_type for event in events] == [JobEventType.HOLD_REQUESTED]


@pytest.mark.parametrize(
    "status",
    [
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.PROMOTED,
        JobStatus.SKIPPED,
        JobStatus.READY_TO_PROMOTE,
    ],
)
def test_hold_rejects_nonrunnable_states(tmp_path: Path, status: JobStatus) -> None:
    engine, job_id = _stored_job(tmp_path, status=status)

    with Session(engine) as session, session.begin(), pytest.raises(JobControlError):
        hold_job(session, job_id=job_id, actor="test", now=datetime.now(UTC))


def test_hold_is_idempotent_for_already_held_job(tmp_path: Path) -> None:
    engine, job_id = _stored_job(tmp_path, status=JobStatus.HELD)

    with Session(engine) as session, session.begin():
        hold_job(session, job_id=job_id, actor="test", now=datetime.now(UTC))

    _job, events = _job_and_events(engine, job_id)
    assert events == []


def test_hold_running_promotion_is_rejected(tmp_path: Path) -> None:
    engine, job_id = _stored_job(
        tmp_path,
        status=JobStatus.ENCODING,
        stage=JobStage.PROMOTE,
    )

    with Session(engine) as session, session.begin(), pytest.raises(JobControlError):
        hold_job(session, job_id=job_id, actor="test", now=datetime.now(UTC))


def test_release_held_job_returns_to_pending_and_clears_request(tmp_path: Path) -> None:
    engine, job_id = _stored_job(tmp_path)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        hold_job(session, job_id=job_id, actor="test", now=now)
    with Session(engine) as session, session.begin():
        changed = release_job(session, job_id=job_id, actor="test", now=now)

    job, events = _job_and_events(engine, job_id)
    assert changed is True
    assert job.status == JobStatus.QUEUED
    assert job.hold_requested_at is None
    assert job.held_at is None
    assert [event.event_type for event in events] == [
        JobEventType.HELD,
        JobEventType.HOLD_RELEASED,
    ]


def test_release_running_job_clears_pending_hold_request(tmp_path: Path) -> None:
    engine, job_id = _stored_job(tmp_path)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        claim_job_stage(session, job_id=job_id, runner_id="runner", now=now)
        hold_job(session, job_id=job_id, actor="test", now=now)
    with Session(engine) as session, session.begin():
        changed = release_job(session, job_id=job_id, actor="test", now=now)

    job, events = _job_and_events(engine, job_id)
    assert changed is True
    assert job.status == JobStatus.ENCODING
    assert job.hold_requested_at is None
    assert [event.event_type for event in events] == [
        JobEventType.HOLD_REQUESTED,
        JobEventType.HOLD_RELEASED,
    ]


def test_release_without_hold_is_noop(tmp_path: Path) -> None:
    engine, job_id = _stored_job(tmp_path)

    with Session(engine) as session, session.begin():
        changed = release_job(session, job_id=job_id, actor="test", now=datetime.now(UTC))

    _job, events = _job_and_events(engine, job_id)
    assert changed is False
    assert events == []


def test_completion_with_cancel_request_marks_attempt_canceled(tmp_path: Path) -> None:
    engine, job_id = _stored_job(tmp_path)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        attempt = claim_job_stage(session, job_id=job_id, runner_id="runner", now=now)
        cancel_job(session, job_id=job_id, actor="test", now=now)
        complete_job_stage(
            session,
            job_id=job_id,
            attempt_id=attempt.id or 0,
            next_stage=JobStage.PLAN,
            now=now,
        )

    with Session(engine) as session:
        job = session.get(Job, job_id)
        attempt = session.exec(select(JobAttempt)).one()

    assert job is not None
    assert job.status == JobStatus.CANCELLED
    assert attempt.status == AttemptStatus.CANCELED


def test_successful_completion_with_hold_advances_then_holds(tmp_path: Path) -> None:
    engine, job_id = _stored_job(tmp_path)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        attempt = claim_job_stage(session, job_id=job_id, runner_id="runner", now=now)
        hold_job(session, job_id=job_id, actor="test", now=now)
        complete_job_stage(
            session,
            job_id=job_id,
            attempt_id=attempt.id or 0,
            next_stage=JobStage.PLAN,
            now=now,
        )

    job, events = _job_and_events(engine, job_id)
    assert job.status == JobStatus.HELD
    assert job.stage == JobStage.PLAN
    assert [event.event_type for event in events] == [
        JobEventType.HOLD_REQUESTED,
        JobEventType.HELD,
    ]


def test_interruption_with_hold_keeps_current_stage_held(tmp_path: Path) -> None:
    engine, job_id = _stored_job(tmp_path, stage=JobStage.ENCODE)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        attempt = claim_job_stage(session, job_id=job_id, runner_id="runner", now=now)
        hold_job(session, job_id=job_id, actor="test", now=now)
        interrupt_job_stage(session, job_id=job_id, attempt_id=attempt.id or 0, now=now)

    job, events = _job_and_events(engine, job_id)
    assert job.status == JobStatus.HELD
    assert job.stage == JobStage.ENCODE
    assert [event.event_type for event in events] == [
        JobEventType.HOLD_REQUESTED,
        JobEventType.HELD,
    ]


def test_failure_clears_pending_hold_request(tmp_path: Path) -> None:
    engine, job_id = _stored_job(tmp_path)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        attempt = claim_job_stage(session, job_id=job_id, runner_id="runner", now=now)
        hold_job(session, job_id=job_id, actor="test", now=now)
        fail_job_stage(
            session,
            job_id=job_id,
            attempt_id=attempt.id or 0,
            error=ValueError("broken"),
            exit_code=1,
            now=now,
        )

    job, _events = _job_and_events(engine, job_id)
    assert job.status == JobStatus.FAILED
    assert job.hold_requested_at is None
    assert job.held_at is None


@pytest.mark.parametrize("status", [JobStatus.QUEUED, JobStatus.HELD])
def test_priority_update_allowed_for_pending_and_held(
    tmp_path: Path,
    status: JobStatus,
) -> None:
    engine, job_id = _stored_job(tmp_path, status=status)
    now = datetime.now(UTC)

    with Session(engine) as session, session.begin():
        update_job_priority(session, job_id=job_id, priority=100, actor="test", now=now)

    job, events = _job_and_events(engine, job_id)
    assert job.priority == 100
    assert [event.event_type for event in events] == [JobEventType.PRIORITY_CHANGED]
    assert events[0].details_json == '{"new_priority":100,"old_priority":0}'


@pytest.mark.parametrize(
    "status",
    [
        JobStatus.ENCODING,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.READY_TO_PROMOTE,
        JobStatus.PROMOTED,
        JobStatus.SKIPPED,
    ],
)
def test_priority_update_rejects_nonqueued_states(tmp_path: Path, status: JobStatus) -> None:
    engine, job_id = _stored_job(tmp_path, status=status)

    with Session(engine) as session, session.begin(), pytest.raises(JobControlError):
        update_job_priority(session, job_id=job_id, priority=5, actor="test", now=datetime.now(UTC))


def test_recovery_cancels_abandoned_job_with_cancel_request(tmp_path: Path) -> None:
    engine, job_id = _running_job_with_attempt(tmp_path, cancel_requested=True)
    now = datetime.now(UTC)

    with Session(engine) as session, session.begin():
        summary = recover_abandoned_jobs(
            session,
            now=now,
            encoded_output_exists=_encoded_output_exists,
        )

    with Session(engine) as session:
        job = session.get(Job, job_id)
        attempt = session.exec(select(JobAttempt)).one()

    assert summary.recovered_jobs == 1
    assert job is not None
    assert job.status == JobStatus.CANCELLED
    assert attempt.status == AttemptStatus.CANCELED


def test_recovery_holds_abandoned_job_with_hold_request(tmp_path: Path) -> None:
    engine, job_id = _running_job_with_attempt(tmp_path, hold_requested=True)
    now = datetime.now(UTC)

    with Session(engine) as session, session.begin():
        recover_abandoned_jobs(session, now=now, encoded_output_exists=_encoded_output_exists)

    with Session(engine) as session:
        job = session.get(Job, job_id)
        attempt = session.exec(select(JobAttempt)).one()

    assert job is not None
    assert job.status == JobStatus.HELD
    assert job.stage == JobStage.ENCODE
    assert attempt.status == AttemptStatus.INTERRUPTED


def test_recovery_returns_plain_abandoned_job_to_pending(tmp_path: Path) -> None:
    engine, job_id = _running_job_with_attempt(tmp_path)

    with Session(engine) as session, session.begin():
        recover_abandoned_jobs(
            session,
            now=datetime.now(UTC),
            encoded_output_exists=_encoded_output_exists,
        )

    with Session(engine) as session:
        job = session.get(Job, job_id)

    assert job is not None
    assert job.status == JobStatus.QUEUED
    assert job.stage == JobStage.ENCODE


def test_recovery_recovers_running_promotion_jobs(tmp_path: Path) -> None:
    engine, job_id = _running_job_with_attempt(tmp_path, stage=JobStage.PROMOTE)

    with Session(engine) as session, session.begin():
        summary = recover_abandoned_jobs(
            session,
            now=datetime.now(UTC),
            encoded_output_exists=_encoded_output_exists,
        )

    with Session(engine) as session:
        job = session.get(Job, job_id)

    assert summary.recovered_jobs == 1
    assert job is not None
    assert job.status == JobStatus.PROMOTING
    assert job.claimed_by is None


def test_restart_recovers_encoded_job(tmp_path: Path) -> None:
    output = tmp_path / "movie.av1.mkv"
    output.write_bytes(b"encoded")
    engine, job_id = _stored_job(
        tmp_path,
        status=JobStatus.ENCODED,
        stage=JobStage.VALIDATE,
        output_path=output,
    )

    with Session(engine) as session, session.begin():
        summary = recover_abandoned_jobs(
            session,
            now=datetime.now(UTC),
            encoded_output_exists=_encoded_output_exists,
        )

    with Session(engine) as session:
        job = session.get(Job, job_id)

    assert summary.recovered_jobs == 1
    assert job is not None
    assert job.status == JobStatus.VALIDATING
    assert job.stage == JobStage.VALIDATE


def test_restart_recovers_ready_to_promote_job(tmp_path: Path) -> None:
    engine, job_id = _stored_job(
        tmp_path,
        status=JobStatus.READY_TO_PROMOTE,
        stage=JobStage.PROMOTE,
    )

    with Session(engine) as session, session.begin():
        summary = recover_abandoned_jobs(
            session,
            now=datetime.now(UTC),
            encoded_output_exists=_encoded_output_exists,
        )

    with Session(engine) as session:
        job = session.get(Job, job_id)

    assert summary.recovered_jobs == 1
    assert job is not None
    assert job.status == JobStatus.READY_TO_PROMOTE
    assert job.claimed_by is None


def test_restart_handles_missing_encoded_file(tmp_path: Path) -> None:
    engine, job_id = _stored_job(
        tmp_path,
        status=JobStatus.ENCODED,
        stage=JobStage.VALIDATE,
        output_path=tmp_path / "missing.av1.mkv",
    )

    with Session(engine) as session, session.begin():
        summary = recover_abandoned_jobs(
            session,
            now=datetime.now(UTC),
            encoded_output_exists=_encoded_output_exists,
        )

    with Session(engine) as session:
        job = session.get(Job, job_id)

    assert summary.recovered_jobs == 1
    assert job is not None
    assert job.status == JobStatus.FAILED
    assert job.last_error_message == "Encoded output was missing during scheduler recovery."


def test_restart_handles_partial_promotion_backup(tmp_path: Path) -> None:
    backup = tmp_path / "movie.mkv.avarch-original"
    backup.write_bytes(b"original")
    engine, job_id = _stored_job(tmp_path, status=JobStatus.PROMOTING, stage=JobStage.PROMOTE)

    with Session(engine) as session, session.begin():
        summary = recover_abandoned_jobs(
            session,
            now=datetime.now(UTC),
            encoded_output_exists=_encoded_output_exists,
        )

    with Session(engine) as session:
        job = session.get(Job, job_id)

    assert summary.recovered_jobs == 1
    assert backup.read_bytes() == b"original"
    assert job is not None
    assert job.status == JobStatus.PROMOTING


def _running_job_with_attempt(
    tmp_path: Path,
    *,
    stage: JobStage = JobStage.ENCODE,
    cancel_requested: bool = False,
    hold_requested: bool = False,
) -> tuple[Engine, int]:
    engine, job_id = _stored_job(tmp_path, stage=stage)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        claim_job_stage(session, job_id=job_id, runner_id="old", now=now)
        job = session.get(Job, job_id)
        assert job is not None
        if cancel_requested:
            job.cancel_requested_at = now
            job.cancel_requested_by = "test"
        if hold_requested:
            job.hold_requested_at = now
            job.hold_requested_by = "test"
        session.add(job)
    return engine, job_id


def _encoded_output_exists(job: Job) -> bool:
    return job.output_path is not None and Path(job.output_path).exists()


def _stored_job(
    tmp_path: Path,
    *,
    status: JobStatus = JobStatus.QUEUED,
    stage: JobStage = JobStage.PROBE,
    output_path: Path | None = None,
) -> tuple[Engine, int]:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)
    with Session(engine) as session:
        media_file = MediaFile(
            path=str(tmp_path / "movie.mkv"),
            size_bytes=1,
            mtime_ns=2,
            device_id=3,
            inode=4,
            fs_fingerprint="fingerprint",
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
            output_path=str(output_path) if output_path is not None else None,
            status=status,
            stage=stage,
            created_at=now,
            updated_at=now,
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        return engine, job.id or 0


def _engine(tmp_path: Path) -> Engine:
    tmp_path.mkdir(parents=True, exist_ok=True)
    engine = create_db_engine(f"sqlite:///{tmp_path / 'avarch.db'}")
    create_db_schema(engine)
    return engine


def _job_and_events(engine: Engine, job_id: int) -> tuple[Job, list[JobEvent]]:
    with Session(engine) as session:
        job = session.get(Job, job_id)
        assert job is not None
        events = list(
            session.exec(
                select(JobEvent)
                .where(
                    JobEvent.job_id == job_id,
                    col(JobEvent.event_type).not_in(_STAGE_EVENT_TYPES),
                )
                .order_by(col(JobEvent.id))
            )
        )
        return job, events
