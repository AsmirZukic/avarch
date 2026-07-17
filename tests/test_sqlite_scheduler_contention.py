from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter, sleep

from sqlalchemy import Engine
from sqlmodel import Session

from avarch.adapters.sqlite.db import create_db_engine, create_db_schema
from avarch.adapters.sqlite.models import (
    Job,
    JobAttempt,
    JobEvent,
    MediaFile,
    MediaFileStatus,
    SchedulerState,
)
from avarch.adapters.sqlite.progress import SqliteProgressStore
from avarch.adapters.sqlite.scheduler_snapshot import SqliteSchedulerSnapshotQuery
from avarch.domain.jobs import AttemptStatus, JobEventType, JobStage, JobStatus, ResourceClass
from avarch.domain.progress import ProgressPhase, ProgressSnapshot, ProgressSource, ProgressUnit
from avarch.domain.scheduler import SchedulerMode


def test_sqlite_scheduler_watchers_do_not_leak_lock_errors_under_write_load(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    database_url = f"sqlite:///{tmp_path / 'avarch.sqlite'}"
    job_id, attempt_id = _seed_running_job(engine, now=now)
    errors: list[BaseException] = []
    snapshot_counts: list[int] = []
    progress_latencies: list[float] = []
    start = threading.Barrier(3)

    def writer() -> None:
        start.wait()
        for index in range(24):
            cycle_started = perf_counter()
            observed = now + timedelta(seconds=index)
            with Session(engine) as session, session.begin():
                SqliteProgressStore(session).save_snapshot(
                    attempt_id=attempt_id,
                    snapshot=_progress(now=observed, current=index, total=24),
                    persisted_at=observed,
                )
                state = session.get(SchedulerState, 1)
                assert state is not None
                state.heartbeat_at = observed
                state.lease_expires_at = observed + timedelta(seconds=30)
                state.updated_at = observed
                session.add(
                    JobEvent(
                        job_id=job_id,
                        attempt_id=attempt_id,
                        event_type=JobEventType.STAGE_STARTED,
                        stage=JobStage.ENCODE,
                        actor="scheduler",
                        created_at=observed,
                    )
                )
            progress_latencies.append(perf_counter() - cycle_started)
            sleep(0.002)

    def watcher() -> None:
        start.wait()
        snapshots = 0
        for index in range(24):
            observed = now + timedelta(seconds=index)
            with Session(engine) as session:
                snapshot = SqliteSchedulerSnapshotQuery(
                    session,
                    workspace_root=str(tmp_path),
                    database_url=database_url,
                    now=lambda observed=observed: observed,
                ).snapshot()
            assert snapshot.active_jobs
            snapshots += 1
            sleep(0.001)
        snapshot_counts.append(snapshots)

    with ThreadPoolExecutor(max_workers=3) as executor:
        futures = [executor.submit(writer), executor.submit(watcher), executor.submit(watcher)]
        for future in futures:
            try:
                future.result(timeout=15)
            except BaseException as exc:  # pragma: no cover - assertion reports exact thread error
                errors.append(exc)

    assert errors == []
    assert snapshot_counts == [24, 24]
    assert len(progress_latencies) == 24
    assert max(progress_latencies) < 1.0


def _engine(tmp_path: Path) -> Engine:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'avarch.sqlite'}")
    create_db_schema(engine)
    return engine


def _seed_running_job(engine: Engine, *, now: datetime) -> tuple[int, int]:
    with Session(engine) as session, session.begin():
        session.add(
            SchedulerState(
                id=1,
                mode=SchedulerMode.RUNNING,
                runner_id="scheduler",
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=30),
                updated_at=now,
            )
        )
        media = MediaFile(
            path="/media/movie.mkv",
            size_bytes=100,
            mtime_ns=1,
            device_id=1,
            inode=1,
            fs_fingerprint="fingerprint",
            status=MediaFileStatus.PRESENT,
            discovered_at=now,
            last_seen_at=now,
        )
        session.add(media)
        session.flush()
        job = Job(
            media_file_id=media.id or 0,
            profile_name="default",
            profile_hash="profile",
            source_fs_fingerprint=media.fs_fingerprint,
            queue_key="queue",
            status=JobStatus.ENCODING,
            stage=JobStage.ENCODE,
            priority=0,
            created_at=now,
            updated_at=now,
            started_at=now,
        )
        session.add(job)
        session.flush()
        attempt = JobAttempt(
            job_id=job.id or 0,
            attempt_number=1,
            stage=JobStage.ENCODE,
            resource_class=ResourceClass.HEAVY_AV1AN,
            status=AttemptStatus.RUNNING,
            runner_id="runner",
            started_at=now,
        )
        session.add(attempt)
        session.flush()
        return job.id or 0, attempt.id or 0


def _progress(*, now: datetime, current: int, total: int) -> ProgressSnapshot:
    return ProgressSnapshot(
        phase=ProgressPhase.ENCODING,
        current=current,
        total=total,
        unit=ProgressUnit.FRAMES,
        rate_per_second=1.0,
        speed_ratio=None,
        source=ProgressSource.AV1AN_OUTPUT,
        message=None,
        phase_started_at=now,
        observed_at=now,
        heartbeat_at=now,
        advanced_at=now,
    )
