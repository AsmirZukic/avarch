from __future__ import annotations

import asyncio
import dataclasses
import inspect
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from sqlmodel import Session, SQLModel

from avarch.config import AppConfig, DatabaseSettings
from avarch.db import create_db_engine, create_db_schema
from avarch.models.db import Job, JobAttempt, JobEvent, MediaFile, MediaFileStatus
from avarch.models.scheduler import (
    AttemptStatus,
    JobEventType,
    JobStage,
    JobStatus,
    ResourceClass,
)
from avarch.tui.backend import LocalTuiBackend, TuiBackend
from avarch.tui.models.common import UiRevision
from avarch.tui.models.dashboard import DashboardSnapshot, QueueTotals, SchedulerSummary
from avarch.tui.models.queue import QueueFilters


def test_dashboard_snapshot_is_immutable() -> None:
    snapshot = DashboardSnapshot(
        revision=_revision(),
        scheduler=SchedulerSummary(
            mode="running",
            lease_state="inactive",
            runner_id=None,
            heartbeat_at=None,
            lease_expires_at=None,
            control_generation=0,
            acknowledged_generation=0,
            cancel_pending=0,
            hold_pending=0,
        ),
        queue_totals=QueueTotals(),
        active_jobs=(),
        recent_failures=(),
        promotion_ready=(),
    )

    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.active_jobs = ()  # type: ignore[misc]


def test_queue_snapshot_contains_no_orm_models(tmp_path: Path) -> None:
    backend = _backend_with_job(tmp_path)

    snapshot = asyncio.run(backend.get_queue_snapshot(QueueFilters()))

    assert snapshot.rows
    assert not _contains_orm_model(snapshot)


def test_job_detail_snapshot_contains_attempts_and_events(tmp_path: Path) -> None:
    backend = _backend_with_job(tmp_path)

    snapshot = asyncio.run(backend.get_job_detail(1))

    assert snapshot.job_id == 1
    assert [attempt.attempt_number for attempt in snapshot.attempts] == [1]
    assert [event.event_type for event in snapshot.events] == [JobEventType.HELD.value]
    assert not _contains_orm_model(snapshot)


def test_backend_protocol_contains_no_cli_subprocess_methods() -> None:
    method_names = {
        name
        for name, member in inspect.getmembers(TuiBackend)
        if inspect.isfunction(member) and not name.startswith("_")
    }

    assert method_names
    assert all("subprocess" not in name for name in method_names)
    assert all("cli" not in name for name in method_names)
    assert all("command" not in name for name in method_names)


def test_snapshot_revision_is_comparable() -> None:
    earlier = _revision(scheduler_generation=1)
    later = _revision(scheduler_generation=2)

    assert earlier < later
    assert earlier != later


def _backend_with_job(tmp_path: Path) -> LocalTuiBackend:
    database_path = tmp_path / "avarch.db"
    database_url = f"sqlite:///{database_path}"
    engine = create_db_engine(database_url)
    create_db_schema(engine)
    now = datetime.now(UTC)
    media_path = tmp_path / "movie.mkv"

    with Session(engine) as session:
        media_file = MediaFile(
            path=str(media_path),
            size_bytes=123,
            mtime_ns=456,
            device_id=1,
            inode=2,
            fs_fingerprint="source-fingerprint",
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
            source_fs_fingerprint="source-fingerprint",
            queue_key="queue-key",
            status=JobStatus.PENDING,
            stage=JobStage.PROBE,
            priority=10,
            attempts=1,
            created_at=now,
            updated_at=now,
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        session.add(
            JobAttempt(
                job_id=job.id or 0,
                attempt_number=1,
                stage=JobStage.PROBE,
                resource_class=ResourceClass.CHEAP,
                status=AttemptStatus.COMPLETED,
                runner_id="runner",
                started_at=now,
                finished_at=now,
            )
        )
        session.add(
            JobEvent(
                job_id=job.id or 0,
                event_type=JobEventType.HELD,
                actor="test",
                reason="inspection",
                created_at=now,
            )
        )
        session.commit()

    config = AppConfig(database=DatabaseSettings(url=database_url))
    return LocalTuiBackend(config=config, config_path=tmp_path / "avarch.toml")


def _revision(scheduler_generation: int = 0) -> UiRevision:
    return UiRevision(
        scheduler_generation=scheduler_generation,
        newest_job_updated_at=None,
        newest_attempt_updated_at=None,
        newest_validation_created_at=None,
        newest_promotion_updated_at=None,
    )


def _contains_orm_model(value: object, seen: set[int] | None = None) -> bool:
    if seen is None:
        seen = set()
    value_id = id(value)
    if value_id in seen:
        return False
    seen.add(value_id)

    if isinstance(value, SQLModel):
        return True
    if dataclasses.is_dataclass(value):
        return any(
            _contains_orm_model(cast(object, getattr(value, field.name)), seen)
            for field in dataclasses.fields(value)
        )
    if isinstance(value, Mapping):
        mapping = cast(Mapping[object, object], value)
        return any(
            _contains_orm_model(key, seen) or _contains_orm_model(item, seen)
            for key, item in mapping.items()
        )
    if isinstance(value, tuple):
        return any(_contains_orm_model(item, seen) for item in cast(tuple[object, ...], value))
    if isinstance(value, list):
        return any(_contains_orm_model(item, seen) for item in cast(list[object], value))
    if isinstance(value, set):
        return any(_contains_orm_model(item, seen) for item in cast(set[object], value))
    if isinstance(value, frozenset):
        return any(_contains_orm_model(item, seen) for item in cast(frozenset[object], value))
    return False
