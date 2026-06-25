from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlmodel import Session

from avarch.adapters.sqlite.db import create_db_engine, create_db_schema
from avarch.adapters.sqlite.models import Job, MediaFile, MediaFileStatus, SchedulerState
from avarch.adapters.sqlite.scheduler_state import (
    SCHEDULER_LEASE_SECONDS,
    SchedulerAlreadyRunningError,
    SchedulerControlError,
    acquire_scheduler_lease,
    drain_scheduler,
    pause_scheduler,
    release_scheduler_lease,
    resume_scheduler,
    stop_scheduler,
)
from avarch.adapters.sqlite.scheduler_status import SqliteSchedulerStatusStore
from avarch.application.scheduler_status import scheduler_status
from avarch.domain.jobs import JobStage, JobStatus
from avarch.domain.scheduler import SchedulerMode


def test_pause_from_running_persists_reason_and_generation(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)

    with Session(engine) as session, session.begin():
        pause_scheduler(session, now=now, reason="maintenance")

    with Session(engine) as session:
        state = session.get(SchedulerState, 1)

    assert state is not None
    assert state.mode == SchedulerMode.PAUSED
    assert state.control_generation == 1
    assert state.control_requested_at == now.replace(tzinfo=None)
    assert state.control_reason == "maintenance"


def test_pause_is_idempotent_when_already_paused(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)

    with Session(engine) as session, session.begin():
        pause_scheduler(session, now=now, reason="first")
    with Session(engine) as session, session.begin():
        pause_scheduler(session, now=now + timedelta(seconds=1), reason="second")

    with Session(engine) as session:
        state = session.get(SchedulerState, 1)

    assert state is not None
    assert state.mode == SchedulerMode.PAUSED
    assert state.control_generation == 1
    assert state.control_reason == "first"


def test_resume_from_paused_returns_to_running(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)

    with Session(engine) as session, session.begin():
        pause_scheduler(session, now=now, reason="maintenance")
    with Session(engine) as session, session.begin():
        resume_scheduler(session, now=now + timedelta(seconds=1))

    with Session(engine) as session:
        state = session.get(SchedulerState, 1)

    assert state is not None
    assert state.mode == SchedulerMode.RUNNING
    assert state.control_generation == 2
    assert state.control_reason is None


def test_resume_is_idempotent_when_already_running(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)

    with Session(engine) as session, session.begin():
        resume_scheduler(session, now=now)

    with Session(engine) as session:
        state = session.get(SchedulerState, 1)

    assert state is not None
    assert state.mode == SchedulerMode.RUNNING
    assert state.control_generation == 0


def test_drain_requires_active_running_scheduler(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)

    _expect_scheduler_error(
        engine,
        SchedulerControlError,
        lambda session: drain_scheduler(session, now=now),
    )

    with Session(engine) as session, session.begin():
        acquire_scheduler_lease(session, runner_id="runner", now=now)
        drain_scheduler(session, now=now, reason="reboot")

    with Session(engine) as session:
        state = session.get(SchedulerState, 1)

    assert state is not None
    assert state.mode == SchedulerMode.DRAINING
    assert state.control_generation == 1
    assert state.control_reason == "reboot"


def test_drain_rejects_paused_draining_and_stopping(tmp_path: Path) -> None:
    for mode in (SchedulerMode.PAUSED, SchedulerMode.DRAINING, SchedulerMode.STOPPING):
        engine = _engine(tmp_path / mode.value)
        now = datetime.now(UTC)
        with Session(engine) as session, session.begin():
            session.add(
                SchedulerState(
                    id=1,
                    mode=mode,
                    runner_id="runner",
                    lease_expires_at=now + timedelta(seconds=30),
                    updated_at=now,
                )
            )
        _expect_scheduler_error(
            engine,
            SchedulerControlError,
            lambda session, now=now: drain_scheduler(session, now=now),
        )


def test_stop_requires_active_scheduler_and_allows_paused(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)

    _expect_scheduler_error(
        engine,
        SchedulerControlError,
        lambda session: stop_scheduler(session, now=now),
    )

    with Session(engine) as session, session.begin():
        session.add(
            SchedulerState(
                id=1,
                mode=SchedulerMode.PAUSED,
                runner_id="runner",
                lease_expires_at=now + timedelta(seconds=30),
                updated_at=now,
            )
        )
    with Session(engine) as session, session.begin():
        stop_scheduler(session, now=now, reason="shutdown")

    with Session(engine) as session:
        state = session.get(SchedulerState, 1)

    assert state is not None
    assert state.mode == SchedulerMode.STOPPING
    assert state.control_reason == "shutdown"


def test_stop_rejects_draining_and_stopping(tmp_path: Path) -> None:
    for mode in (SchedulerMode.DRAINING, SchedulerMode.STOPPING):
        engine = _engine(tmp_path / mode.value)
        now = datetime.now(UTC)
        with Session(engine) as session, session.begin():
            session.add(
                SchedulerState(
                    id=1,
                    mode=mode,
                    runner_id="runner",
                    lease_expires_at=now + timedelta(seconds=30),
                    updated_at=now,
                )
            )
        _expect_scheduler_error(
            engine,
            SchedulerControlError,
            lambda session, now=now: stop_scheduler(session, now=now),
        )


def test_acquire_lease_refuses_paused_without_resume(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        pause_scheduler(session, now=now)

    _expect_scheduler_error(
        engine,
        SchedulerControlError,
        lambda session: acquire_scheduler_lease(session, runner_id="runner", now=now),
    )


def test_acquire_lease_resume_overrides_paused_state(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        pause_scheduler(session, now=now)
    with Session(engine) as session, session.begin():
        acquire_scheduler_lease(
            session,
            runner_id="runner",
            now=now + timedelta(seconds=1),
            resume=True,
        )

    with Session(engine) as session:
        state = session.get(SchedulerState, 1)

    assert state is not None
    assert state.mode == SchedulerMode.RUNNING
    assert state.runner_id == "runner"
    assert state.control_generation == 2


def test_acquire_lease_rejects_active_other_runner(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        acquire_scheduler_lease(session, runner_id="first", now=now)

    _expect_scheduler_error(
        engine,
        SchedulerAlreadyRunningError,
        lambda session: acquire_scheduler_lease(session, runner_id="second", now=now),
    )


def test_stale_one_shot_mode_resets_on_acquire(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        session.add(
            SchedulerState(
                id=1,
                mode=SchedulerMode.STOPPING,
                runner_id="old",
                lease_expires_at=now - timedelta(seconds=1),
                control_generation=3,
                control_requested_at=now - timedelta(minutes=1),
                control_reason="old stop",
                updated_at=now,
            )
        )
    with Session(engine) as session, session.begin():
        acquire_scheduler_lease(session, runner_id="new", now=now)

    with Session(engine) as session:
        state = session.get(SchedulerState, 1)

    assert state is not None
    assert state.mode == SchedulerMode.RUNNING
    assert state.runner_id == "new"
    assert state.control_generation == 3
    assert state.control_requested_at is None
    assert state.control_reason is None


def test_release_one_shot_mode_resets_to_running(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        acquire_scheduler_lease(session, runner_id="runner", now=now)
        drain_scheduler(session, now=now)
    with Session(engine) as session, session.begin():
        release_scheduler_lease(session, runner_id="runner", now=now)

    with Session(engine) as session:
        state = session.get(SchedulerState, 1)

    assert state is not None
    assert state.mode == SchedulerMode.RUNNING
    assert state.runner_id is None
    assert state.lease_expires_at is None
    assert state.control_generation == 1


def test_scheduler_status_reports_counts_pending_controls_and_lease_state(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        _store_job(session, tmp_path, "pending", status=JobStatus.QUEUED, now=now)
        running = _store_job(session, tmp_path, "running", status=JobStatus.ENCODING, now=now)
        held = _store_job(session, tmp_path, "held", status=JobStatus.HELD, now=now)
        running.cancel_requested_at = now
        held.hold_requested_at = now
        acquire_scheduler_lease(session, runner_id="runner", now=now)

    with Session(engine) as session:
        status = scheduler_status(SqliteSchedulerStatusStore(session), now=now)

    assert status.lease_state == "active"
    assert status.runner_id == "runner"
    assert status.counts_by_status[JobStatus.QUEUED] == 1
    assert status.counts_by_status[JobStatus.ENCODING] == 1
    assert status.counts_by_status[JobStatus.HELD] == 1
    assert status.cancel_pending == 1
    assert status.hold_pending == 0
    assert [job.file_name for job in status.active_jobs] == ["running.mkv"]


def test_scheduler_status_reports_stale_lease(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        session.add(
            SchedulerState(
                id=1,
                mode=SchedulerMode.RUNNING,
                runner_id="old",
                lease_expires_at=now - timedelta(seconds=1),
                updated_at=now,
            )
        )

    with Session(engine) as session:
        status = scheduler_status(SqliteSchedulerStatusStore(session), now=now)

    assert status.lease_state == "stale"


def test_acquired_lease_sets_expected_expiry(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)

    with Session(engine) as session, session.begin():
        acquire_scheduler_lease(session, runner_id="runner", now=now)
    with Session(engine) as session:
        state = session.get(SchedulerState, 1)

    assert state is not None
    assert state.lease_expires_at == (now + timedelta(seconds=SCHEDULER_LEASE_SECONDS)).replace(
        tzinfo=None
    )


def _engine(tmp_path: Path) -> Engine:
    tmp_path.mkdir(parents=True, exist_ok=True)
    engine = create_db_engine(f"sqlite:///{tmp_path / 'avarch.adapters.sqlite.db'}")
    create_db_schema(engine)
    return engine


def _expect_scheduler_error(
    engine: Engine,
    error_type: type[Exception],
    action: Callable[[Session], object],
) -> None:
    try:
        with Session(engine) as session, session.begin():
            action(session)
    except error_type:
        return
    pytest.fail(f"Expected {error_type.__name__}")


def _store_job(
    session: Session,
    tmp_path: Path,
    profile_name: str,
    *,
    status: JobStatus,
    now: datetime,
) -> Job:
    media_file = MediaFile(
        path=str(tmp_path / f"{profile_name}.mkv"),
        size_bytes=1,
        mtime_ns=2,
        device_id=3,
        inode=4,
        fs_fingerprint=f"{profile_name}-fingerprint",
        discovered_at=now,
        last_seen_at=now,
        status=MediaFileStatus.PRESENT,
    )
    session.add(media_file)
    session.flush()
    job = Job(
        media_file_id=media_file.id or 0,
        profile_name=profile_name,
        profile_hash="profile-hash",
        source_fs_fingerprint=media_file.fs_fingerprint,
        queue_key=f"{profile_name}-queue",
        status=status,
        stage=JobStage.PROBE,
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    session.flush()
    return job
