from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session, select

from avarch.adapters.sqlite.db import create_db_engine, create_db_schema
from avarch.adapters.sqlite.models import (
    Job,
    JobAttempt,
    JobEvent,
    MediaFile,
    MediaFileStatus,
    ProbeResult,
    SchedulerSession,
)
from avarch.domain.jobs import (
    AttemptStatus,
    JobEventType,
    JobStage,
    JobStatus,
    ResourceClass,
)


def test_job_event_metadata_is_nullable_for_historical_events(tmp_path: Path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'avarch.sqlite'}")
    create_db_schema(engine)
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)

    with Session(engine) as session:
        job_id, _attempt_id, _session_id = _insert_job_attempt_and_session(session, now=now)
        session.add(
            JobEvent(
                job_id=job_id,
                event_type=JobEventType.HOLD_REQUESTED,
                actor="operator",
                reason="manual hold",
                created_at=now,
            )
        )
        session.commit()

        event = session.exec(select(JobEvent)).one()

    assert event.attempt_id is None
    assert event.scheduler_session_id is None
    assert event.stage is None
    assert event.details_json is None
    assert event.dedupe_key is None


def test_job_event_round_trips_attempt_session_stage_and_details(tmp_path: Path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'avarch.sqlite'}")
    create_db_schema(engine)
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)

    with Session(engine) as session:
        job_id, attempt_id, session_id = _insert_job_attempt_and_session(session, now=now)
        session.add(
            JobEvent(
                job_id=job_id,
                attempt_id=attempt_id,
                scheduler_session_id=session_id,
                event_type=JobEventType.RETRY_REQUESTED,
                stage=JobStage.ENCODE,
                actor="scheduler",
                details_json='{"error_code":"encoder_exit","exit_code":1}',
                dedupe_key="attempt-1-encode-failed",
                created_at=now,
            )
        )
        session.commit()

        event = session.exec(select(JobEvent)).one()

    assert event.attempt_id == attempt_id
    assert event.scheduler_session_id == session_id
    assert event.stage == JobStage.ENCODE
    assert event.details_json == '{"error_code":"encoder_exit","exit_code":1}'
    assert event.dedupe_key == "attempt-1-encode-failed"


def _insert_job_attempt_and_session(
    session: Session,
    *,
    now: datetime,
) -> tuple[int, int, int]:
    media_file = MediaFile(
        path="/media/movie.mkv",
        size_bytes=123,
        mtime_ns=456,
        device_id=789,
        inode=101112,
        fs_fingerprint="source-fs",
        discovered_at=now,
        last_seen_at=now,
        status=MediaFileStatus.PRESENT,
    )
    session.add(media_file)
    session.flush()

    probe = ProbeResult(
        media_file_id=media_file.id or 0,
        ffprobe_json="{}",
        normalized_json="{}",
        probe_hash="probe",
        source_fs_fingerprint=media_file.fs_fingerprint,
        created_at=now,
    )
    session.add(probe)
    session.flush()

    job = Job(
        media_file_id=media_file.id or 0,
        profile_name="default",
        profile_hash="profile",
        source_fs_fingerprint=media_file.fs_fingerprint,
        queue_key="queue",
        probe_result_id=probe.id,
        probe_hash=probe.probe_hash,
        status=JobStatus.ENCODING,
        stage=JobStage.ENCODE,
        priority=0,
        attempts=1,
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    session.flush()

    scheduler_session = SchedulerSession(
        owner_id="runner",
        workspace_id="workspace",
        pid=1234,
        host="host",
        started_at=now,
    )
    session.add(scheduler_session)
    session.flush()

    attempt = JobAttempt(
        job_id=job.id or 0,
        scheduler_session_id=scheduler_session.id,
        attempt_number=1,
        stage=JobStage.ENCODE,
        resource_class=ResourceClass.HEAVY_AV1AN,
        status=AttemptStatus.RUNNING,
        runner_id="runner",
        started_at=now,
    )
    session.add(attempt)
    session.flush()

    return job.id or 0, attempt.id or 0, scheduler_session.id or 0
