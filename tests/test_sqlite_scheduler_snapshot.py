from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine, event
from sqlmodel import Session

from avarch.adapters.sqlite.db import create_db_engine, create_db_schema
from avarch.adapters.sqlite.models import (
    Job,
    JobAttempt,
    JobEvent,
    MediaFile,
    MediaFileStatus,
    SchedulerSession,
    SchedulerState,
)
from avarch.adapters.sqlite.progress import SqliteProgressStore
from avarch.adapters.sqlite.scheduler_snapshot import SqliteSchedulerSnapshotQuery
from avarch.application.scheduler_blockers import JobEligibilityReason
from avarch.domain.jobs import AttemptStatus, JobEventType, JobStage, JobStatus, ResourceClass
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
            snapshot=_progress(
                now=now - timedelta(seconds=18),
                current=40,
                total=100,
                chunks_current=2,
                chunks_total=5,
                bitrate_kbps=1500,
                estimated_output_bytes=2_000_000,
                written_output_bytes=800_000,
            ),
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
    assert snapshot.active_jobs[0].attempt.chunks_current == 2
    assert snapshot.active_jobs[0].attempt.chunks_total == 5
    assert snapshot.active_jobs[0].attempt.bitrate_kbps == 1500
    assert snapshot.active_jobs[0].attempt.estimated_output_bytes == 2_000_000
    assert snapshot.active_jobs[0].attempt.written_output_bytes == 800_000
    assert snapshot.active_jobs[0].attempt.stale is True
    assert snapshot.active_jobs[0].attempt.last_update_age_seconds == 18
    assert snapshot.active_jobs[0].workflow_steps[3].state.value == "active"
    assert query_count <= 13


def test_scheduler_snapshot_does_not_rewind_encode_job_to_scene_detect_for_stale_scene_progress(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)

    with Session(engine) as session:
        job = _job(
            session,
            path="/media/active-scene-detect.mkv",
            status=JobStatus.ENCODING,
            stage=JobStage.ENCODE,
            started_at=now,
        )
        attempt = _attempt(
            job,
            attempt_number=1,
            status=AttemptStatus.RUNNING,
            started_at=now,
            stage=JobStage.ENCODE,
            resource_class=ResourceClass.HEAVY_AV1AN,
        )
        session.add(attempt)
        session.flush()
        SqliteProgressStore(session).save_snapshot(
            attempt_id=attempt.id or 0,
            snapshot=_progress(
                now=now,
                current=40,
                total=100,
                phase=ProgressPhase.SCENE_DETECTION,
            ),
            persisted_at=now,
        )
        session.commit()

    with Session(engine) as session:
        snapshot = SqliteSchedulerSnapshotQuery(
            session,
            workspace_root=str(tmp_path),
            now=lambda: now,
        ).snapshot()

    active = snapshot.active_jobs[0]
    assert active.stage == JobStage.ENCODE
    assert active.attempt is not None
    assert active.attempt.phase == ProgressPhase.ENCODING
    assert active.attempt.message == "telemetry pending"
    assert active.attempt.frames_current is None
    assert active.workflow_steps[2].stage == JobStage.SCENE_DETECT
    assert active.workflow_steps[2].state.value == "complete"
    assert active.workflow_steps[3].stage == JobStage.ENCODE
    assert active.workflow_steps[3].state.value == "active"


def test_scheduler_snapshot_includes_ordered_upcoming_jobs_and_filters_ineligible(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)

    with Session(engine) as session:
        _job(
            session,
            path="/media/third.mkv",
            status=JobStatus.QUEUED,
            stage=JobStage.ENCODE,
            priority=10,
            created_at=now - timedelta(minutes=3),
            profile_name="slow-av1",
        )
        _job(
            session,
            path="/media/first.mkv",
            status=JobStatus.QUEUED,
            stage=JobStage.PLAN,
            priority=20,
            created_at=now - timedelta(minutes=1),
            profile_name="fast-av1",
        )
        _job(
            session,
            path="/media/second.mkv",
            status=JobStatus.QUEUED,
            stage=JobStage.PROBE,
            priority=20,
            created_at=now,
            profile_name="fast-av1",
        )
        _job(
            session,
            path="/media/fourth.mkv",
            status=JobStatus.QUEUED,
            stage=JobStage.ENCODE,
            priority=1,
            created_at=now - timedelta(minutes=4),
        )
        _job(
            session,
            path="/media/held.mkv",
            status=JobStatus.QUEUED,
            stage=JobStage.ENCODE,
            priority=100,
            hold_requested_at=now,
        )
        _job(
            session,
            path="/media/cancel-requested.mkv",
            status=JobStatus.QUEUED,
            stage=JobStage.ENCODE,
            priority=100,
            cancel_requested_at=now,
        )
        session.commit()

    with Session(engine) as session:
        snapshot = SqliteSchedulerSnapshotQuery(
            session,
            workspace_root=str(tmp_path),
            now=lambda: now,
        ).snapshot()

    assert [job.source_path for job in snapshot.upcoming_jobs] == [
        "/media/first.mkv",
        "/media/second.mkv",
        "/media/third.mkv",
    ]
    assert [job.selection_position for job in snapshot.upcoming_jobs] == [1, 2, 3]
    assert snapshot.upcoming_jobs[0].profile_name == "fast-av1"
    assert snapshot.upcoming_jobs[0].selection_confidence == "current_snapshot"
    assert all("held" not in job.source_path for job in snapshot.upcoming_jobs)
    assert all("cancel-requested" not in job.source_path for job in snapshot.upcoming_jobs)


def test_scheduler_snapshot_upcoming_jobs_reuse_capacity_selection(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)

    with Session(engine) as session:
        session.add(
            SchedulerState(
                id=1,
                mode=SchedulerMode.RUNNING,
                runner_id="scheduler-1",
                heartbeat_at=now.replace(tzinfo=None),
                lease_expires_at=(now + timedelta(seconds=30)).replace(tzinfo=None),
                capacity_cheap_workers=1,
                capacity_cheap_active=0,
                capacity_av1an_jobs=1,
                capacity_av1an_active=1,
                capacity_file_ops=1,
                capacity_file_ops_active=0,
                capacity_observed_at=now.replace(tzinfo=None),
                updated_at=now.replace(tzinfo=None),
            )
        )
        running_encode = _job(
            session,
            path="/media/running-encode.mkv",
            status=JobStatus.ENCODING,
            stage=JobStage.ENCODE,
            priority=100,
            created_at=now - timedelta(minutes=5),
        )
        running_attempt = _attempt(
            running_encode,
            attempt_number=1,
            status=AttemptStatus.RUNNING,
            started_at=now - timedelta(minutes=4),
        )
        running_attempt.command_json = json.dumps(
            {"av1an_argv": ["av1an", "-i", "source.mkv", "--workers", "4"]}
        )
        session.add(running_attempt)
        _job(
            session,
            path="/media/would-encode-next.mkv",
            status=JobStatus.QUEUED,
            stage=JobStage.ENCODE,
            priority=90,
            created_at=now - timedelta(minutes=4),
        )
        _job(
            session,
            path="/media/can-validate.mkv",
            status=JobStatus.ENCODED,
            stage=JobStage.VALIDATE,
            priority=80,
            created_at=now - timedelta(minutes=3),
        )
        _job(
            session,
            path="/media/can-promote.mkv",
            status=JobStatus.READY_TO_PROMOTE,
            stage=JobStage.PROMOTE,
            priority=70,
            created_at=now - timedelta(minutes=2),
        )
        session.commit()

    with Session(engine) as session:
        snapshot = SqliteSchedulerSnapshotQuery(
            session,
            workspace_root=str(tmp_path),
            now=lambda: now,
        ).snapshot()

    assert [job.source_path for job in snapshot.upcoming_jobs] == [
        "/media/can-validate.mkv",
        "/media/can-promote.mkv",
    ]
    assert [job.selection_confidence for job in snapshot.upcoming_jobs] == [
        "current_snapshot",
        "current_snapshot",
    ]
    assert snapshot.capacity.av1an_workers_configured == 4


def test_scheduler_snapshot_alerts_for_stale_paused_and_draining_modes(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)

    with Session(engine) as session:
        session.add(
            SchedulerState(
                id=1,
                mode=SchedulerMode.RUNNING,
                runner_id="scheduler-1",
                heartbeat_at=(now - timedelta(minutes=5)).replace(tzinfo=None),
                lease_expires_at=(now - timedelta(minutes=4)).replace(tzinfo=None),
                updated_at=(now - timedelta(minutes=5)).replace(tzinfo=None),
            )
        )
        session.commit()

        stale = SqliteSchedulerSnapshotQuery(
            session,
            workspace_root=str(tmp_path),
            now=lambda: now,
        ).snapshot()
        state = session.get(SchedulerState, 1)
        assert state is not None
        state.runner_id = None
        state.mode = SchedulerMode.PAUSED
        session.add(state)
        session.commit()

        paused = SqliteSchedulerSnapshotQuery(
            session,
            workspace_root=str(tmp_path),
            now=lambda: now,
        ).snapshot()
        state.mode = SchedulerMode.DRAINING
        session.add(state)
        session.commit()

        draining = SqliteSchedulerSnapshotQuery(
            session,
            workspace_root=str(tmp_path),
            now=lambda: now,
        ).snapshot()

    assert stale.scheduler.state.value == "stale"
    assert [alert.code for alert in stale.alerts] == ["stale_scheduler_lease"]
    assert paused.scheduler.state.value == "paused"
    assert [alert.code for alert in paused.alerts] == ["scheduler_paused"]
    assert draining.scheduler.state.value == "draining"
    assert [alert.code for alert in draining.alerts] == ["scheduler_draining"]
    assert stale.resources is None
    assert stale.forecast is None
    assert stale.recent_events == ()
    assert stale.session is None


def test_scheduler_snapshot_includes_session_history_and_recent_events(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)

    with Session(engine) as session:
        job = _job(
            session,
            path="/media/movie.mkv",
            status=JobStatus.ENCODING,
            stage=JobStage.ENCODE,
        )
        ended = SchedulerSession(
            owner_id="old-runner",
            workspace_id="workspace",
            pid=111,
            host="old-host",
            started_at=(now - timedelta(hours=2)).replace(tzinfo=None),
            ended_at=(now - timedelta(hours=1)).replace(tzinfo=None),
            end_reason="normal",
        )
        current = SchedulerSession(
            owner_id="runner-1",
            workspace_id="workspace",
            pid=222,
            host="host",
            started_at=(now - timedelta(minutes=10)).replace(tzinfo=None),
        )
        session.add_all([ended, current])
        session.flush()
        attempt = _attempt(
            job,
            attempt_number=1,
            status=AttemptStatus.RUNNING,
            started_at=now - timedelta(minutes=9),
        )
        attempt.scheduler_session_id = current.id
        session.add(attempt)
        session.flush()
        session.add(
            JobEvent(
                job_id=job.id or 0,
                attempt_id=attempt.id,
                scheduler_session_id=current.id,
                event_type=JobEventType.STAGE_COMPLETED,
                stage=JobStage.ENCODE,
                actor="runner-1",
                details_json='{"frames":100,"saved_bytes":42}',
                created_at=(now - timedelta(minutes=1)).replace(tzinfo=None),
            )
        )
        session.add(
            JobEvent(
                job_id=job.id or 0,
                event_type=JobEventType.HOLD_REQUESTED,
                actor="operator",
                reason="pause",
                created_at=(now - timedelta(minutes=2)).replace(tzinfo=None),
            )
        )
        session.commit()

    with Session(engine) as session:
        snapshot = SqliteSchedulerSnapshotQuery(
            session,
            workspace_root=str(tmp_path),
            now=lambda: now,
        ).snapshot()

    assert snapshot.session is not None
    assert snapshot.session.current is not None
    assert snapshot.session.current.owner_id == "runner-1"
    assert snapshot.session.current.pid == 222
    assert snapshot.session.current.active is True
    assert [run.owner_id for run in snapshot.session.recent] == ["runner-1", "old-runner"]
    assert snapshot.session.recent[1].end_reason == "normal"
    assert [event.event_type for event in snapshot.recent_events] == [
        JobEventType.STAGE_COMPLETED,
        JobEventType.HOLD_REQUESTED,
    ]
    assert snapshot.recent_events[0].stage == JobStage.ENCODE
    assert snapshot.recent_events[0].scheduler_session_id == snapshot.session.current.session_id
    assert snapshot.recent_events[0].details == {"frames": 100, "saved_bytes": 42}


def test_scheduler_snapshot_projects_known_blockers(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)

    with Session(engine) as session:
        held = _job(
            session,
            path="/media/held.mkv",
            status=JobStatus.QUEUED,
            stage=JobStage.ENCODE,
            priority=40,
            hold_requested_at=now,
        )
        held.hold_reason = "inspect"
        cancelled = _job(
            session,
            path="/media/cancel.mkv",
            status=JobStatus.QUEUED,
            stage=JobStage.ENCODE,
            priority=30,
            cancel_requested_at=now,
        )
        cancelled.cancel_reason = "operator"
        missing = _job(
            session,
            path="/media/missing.mkv",
            status=JobStatus.QUEUED,
            stage=JobStage.PROBE,
            priority=20,
        )
        media = session.get(MediaFile, missing.media_file_id)
        assert media is not None
        media.status = MediaFileStatus.MISSING
        _job(
            session,
            path="/media/no-plan.mkv",
            status=JobStatus.QUEUED,
            stage=JobStage.ENCODE,
            priority=10,
        )
        session.add_all([held, cancelled, media])
        session.commit()

    with Session(engine) as session:
        snapshot = SqliteSchedulerSnapshotQuery(
            session,
            workspace_root=str(tmp_path),
            now=lambda: now,
        ).snapshot()

    assert [(job.source_path, job.reason) for job in snapshot.blocked_jobs] == [
        ("/media/held.mkv", JobEligibilityReason.JOB_HELD),
        ("/media/cancel.mkv", JobEligibilityReason.CANCEL_REQUESTED),
        ("/media/missing.mkv", JobEligibilityReason.SOURCE_MISSING),
        ("/media/no-plan.mkv", JobEligibilityReason.PLAN_MISSING),
    ]
    assert snapshot.blocked_jobs[0].details == {"hold_reason": "inspect"}
    assert snapshot.blocked_jobs[1].details == {"cancel_reason": "operator"}


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
    created_at: datetime | None = None,
    profile_name: str = "default",
    hold_requested_at: datetime | None = None,
    cancel_requested_at: datetime | None = None,
) -> Job:
    now = created_at or datetime(2026, 7, 17, 11, 0, tzinfo=UTC)
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
        profile_name=profile_name,
        profile_hash="profile",
        source_fs_fingerprint=media.fs_fingerprint,
        queue_key=f"queue-{path}",
        status=status,
        stage=stage,
        priority=priority,
        created_at=now.replace(tzinfo=None),
        updated_at=now.replace(tzinfo=None),
        started_at=started_at.replace(tzinfo=None) if started_at else None,
        hold_requested_at=hold_requested_at.replace(tzinfo=None) if hold_requested_at else None,
        cancel_requested_at=cancel_requested_at.replace(tzinfo=None)
        if cancel_requested_at
        else None,
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


def _progress(
    *,
    now: datetime,
    current: int,
    total: int,
    phase: ProgressPhase = ProgressPhase.ENCODING,
    chunks_current: int | None = None,
    chunks_total: int | None = None,
    bitrate_kbps: int | None = None,
    estimated_output_bytes: int | None = None,
    written_output_bytes: int | None = None,
) -> ProgressSnapshot:
    return ProgressSnapshot(
        phase=phase,
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
        chunks_current=chunks_current,
        chunks_total=chunks_total,
        bitrate_kbps=bitrate_kbps,
        estimated_output_bytes=estimated_output_bytes,
        written_output_bytes=written_output_bytes,
    )
