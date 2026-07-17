from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine, event
from sqlmodel import Session

from avarch.adapters.sqlite.db import create_db_engine, create_db_schema
from avarch.adapters.sqlite.models import (
    Job,
    JobAttempt,
    MediaFile,
    MediaFileStatus,
    SchedulerState,
)
from avarch.adapters.sqlite.progress import SqliteProgressStore
from avarch.adapters.sqlite.scheduler_snapshot import SqliteSchedulerSnapshotQuery
from avarch.domain.jobs import AttemptStatus, JobStage, JobStatus, ResourceClass
from avarch.domain.progress import ProgressPhase, ProgressSnapshot, ProgressSource, ProgressUnit
from avarch.domain.scheduler import SchedulerMode


def test_scheduler_snapshot_counts_active_attempts_and_current_progress(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    old = now - timedelta(minutes=10)

    with Session(engine) as session:
        session.add(
            SchedulerState(
                id=1,
                mode=SchedulerMode.RUNNING,
                runner_id="scheduler-1",
                heartbeat_at=now.replace(tzinfo=None),
                lease_expires_at=(now + timedelta(seconds=30)).replace(tzinfo=None),
                updated_at=now.replace(tzinfo=None),
            )
        )
        _job(session, path="/media/queued.mkv", status=JobStatus.QUEUED, stage=JobStage.PROBE)
        first_active = _job(
            session,
            path="/media/active-a.mkv",
            status=JobStatus.ENCODING,
            stage=JobStage.ENCODE,
            priority=5,
            started_at=old,
        )
        second_active = _job(
            session,
            path="/media/active-b.mkv",
            status=JobStatus.VALIDATING,
            stage=JobStage.VALIDATE,
            priority=1,
            started_at=old,
        )
        _job(session, path="/media/promoted.mkv", status=JobStatus.PROMOTED, stage=JobStage.PROMOTE)
        _job(session, path="/media/failed.mkv", status=JobStatus.FAILED, stage=JobStage.ENCODE)
        _job(
            session,
            path="/media/validation-failed.mkv",
            status=JobStatus.VALIDATION_FAILED,
            stage=JobStage.VALIDATE,
        )
        _job(
            session,
            path="/media/rejected.mkv",
            status=JobStatus.SIZE_REJECTED,
            stage=JobStage.PROMOTE,
        )
        _job(
            session,
            path="/media/cancelled.mkv",
            status=JobStatus.CANCELLED,
            stage=JobStage.ENCODE,
        )
        session.flush()

        stale_attempt = _attempt(
            first_active,
            attempt_number=1,
            status=AttemptStatus.FAILED,
            started_at=old,
        )
        current_attempt = _attempt(
            first_active,
            attempt_number=2,
            status=AttemptStatus.RUNNING,
            started_at=old + timedelta(minutes=1),
        )
        validation_attempt = _attempt(
            second_active,
            attempt_number=1,
            status=AttemptStatus.RUNNING,
            started_at=old + timedelta(minutes=2),
            stage=JobStage.VALIDATE,
            resource_class=ResourceClass.CHEAP,
        )
        session.add_all([stale_attempt, current_attempt, validation_attempt])
        session.flush()

        progress_store = SqliteProgressStore(session)
        progress_store.save_snapshot(
            attempt_id=stale_attempt.id or 0,
            snapshot=_progress(now=now, current=999, total=1000),
            persisted_at=now,
        )
        progress_store.save_snapshot(
            attempt_id=current_attempt.id or 0,
            snapshot=_progress(now=now, current=40, total=100),
            persisted_at=now,
        )
        session.commit()

    query_count = 0

    def count_query(*_args: object) -> None:
        nonlocal query_count
        query_count += 1

    event.listen(engine, "before_cursor_execute", count_query)
    try:
        with Session(engine) as session:
            snapshot = SqliteSchedulerSnapshotQuery(
                session,
                workspace_root=str(tmp_path),
                database_url=f"sqlite:///{tmp_path / 'avarch.sqlite'}",
                now=lambda: now,
            ).snapshot()
    finally:
        event.remove(engine, "before_cursor_execute", count_query)

    assert snapshot.captured_at == now
    assert snapshot.workspace.root_path == str(tmp_path)
    assert snapshot.scheduler.state.value == "running"
    assert snapshot.pipeline.queued == 1
    assert snapshot.pipeline.active == 2
    assert snapshot.pipeline.completed == 1
    assert snapshot.pipeline.failed == 1
    assert snapshot.pipeline.validation_failed == 1
    assert snapshot.pipeline.size_rejected == 1
    assert snapshot.pipeline.cancelled == 1
    assert [job.source_path for job in snapshot.active_jobs] == [
        "/media/active-a.mkv",
        "/media/active-b.mkv",
    ]
    assert snapshot.active_jobs[0].attempt is not None
    assert snapshot.active_jobs[0].attempt.attempt_number == 2
    assert snapshot.active_jobs[0].attempt.frames_current == 40
    assert snapshot.active_jobs[0].attempt.frames_total == 100
    assert snapshot.active_jobs[0].attempt.eta_seconds == 30
    assert snapshot.active_jobs[0].workflow_steps[2].state.value == "active"
    assert query_count <= 6


def test_scheduler_snapshot_reports_stopped_when_no_scheduler_process(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)

    with Session(engine) as session:
        snapshot = SqliteSchedulerSnapshotQuery(
            session,
            workspace_root=str(tmp_path),
            now=lambda: now,
        ).snapshot()

    assert snapshot.scheduler.state.value == "stopped"
    assert snapshot.active_jobs == ()


def _engine(tmp_path: Path) -> Engine:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'avarch.sqlite'}")
    create_db_schema(engine)
    return engine


def _job(
    session: Session,
    *,
    path: str,
    status: JobStatus,
    stage: JobStage,
    priority: int = 0,
    started_at: datetime | None = None,
) -> Job:
    now = datetime(2026, 7, 17, 11, 0, tzinfo=UTC)
    media = MediaFile(
        path=path,
        size_bytes=100,
        mtime_ns=1,
        device_id=1,
        inode=len(path),
        fs_fingerprint=f"fingerprint-{path}",
        discovered_at=now,
        last_seen_at=now,
        status=MediaFileStatus.PRESENT,
    )
    session.add(media)
    session.flush()
    job = Job(
        media_file_id=media.id or 0,
        profile_name="default",
        profile_hash="profile",
        source_fs_fingerprint=media.fs_fingerprint,
        queue_key=f"queue-{path}",
        status=status,
        stage=stage,
        priority=priority,
        created_at=now.replace(tzinfo=None),
        updated_at=now.replace(tzinfo=None),
        started_at=started_at.replace(tzinfo=None) if started_at else None,
    )
    session.add(job)
    session.flush()
    return job


def _attempt(
    job: Job,
    *,
    attempt_number: int,
    status: AttemptStatus,
    started_at: datetime,
    stage: JobStage = JobStage.ENCODE,
    resource_class: ResourceClass = ResourceClass.HEAVY_AV1AN,
) -> JobAttempt:
    return JobAttempt(
        job_id=job.id or 0,
        attempt_number=attempt_number,
        stage=stage,
        resource_class=resource_class,
        status=status,
        runner_id="runner",
        started_at=started_at.replace(tzinfo=None),
    )


def _progress(*, now: datetime, current: int, total: int) -> ProgressSnapshot:
    return ProgressSnapshot(
        phase=ProgressPhase.ENCODING,
        current=current,
        total=total,
        unit=ProgressUnit.FRAMES,
        rate_per_second=2.0,
        speed_ratio=1.25,
        source=ProgressSource.AV1AN_OUTPUT,
        message=None,
        phase_started_at=now.replace(tzinfo=None),
        observed_at=now.replace(tzinfo=None),
        heartbeat_at=now.replace(tzinfo=None),
        advanced_at=now.replace(tzinfo=None),
    )
