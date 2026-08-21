from __future__ import annotations

# pyright: reportPrivateUsage=false
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine
from sqlmodel import Session, col, select

from avarch.adapters import scheduler_workers
from avarch.adapters.execution import (
    ProcessOutputRecord,
    _Av1anProgressCollector,
)
from avarch.adapters.progress.av1an_tty import Av1anTtyProgressParser
from avarch.adapters.scheduler_workers import (
    _restore_missing_scene_detect_stage,
    _SceneDetectStageProgressSink,
)
from avarch.adapters.sqlite.db import create_db_engine, create_db_schema
from avarch.adapters.sqlite.job_transitions import claim_job_stage
from avarch.adapters.sqlite.models import Job, JobEvent, MediaFile, MediaFileStatus
from avarch.application.progress import ProgressSink
from avarch.domain.jobs import JobEventType, JobStage, JobStatus
from avarch.domain.progress import ProgressPhase, ProgressSnapshot, ProgressSource, ProgressUnit

NOW = datetime(2026, 7, 17, 13, 44, tzinfo=UTC)
AV1AN_FIXTURE = Path(__file__).parent / "fixtures" / "av1an_progress" / "encode_tty_raw.bin"


class _CaptureSink(ProgressSink):
    def __init__(self) -> None:
        self.snapshots: list[ProgressSnapshot] = []

    def publish(self, snapshot: ProgressSnapshot) -> None:
        self.snapshots.append(snapshot)


def test_real_av1an_stream_advances_scene_detect_to_encode_once(tmp_path: Path) -> None:
    engine, job_id, attempt_id = _claimed_scene_detect_job(tmp_path)
    downstream = _CaptureSink()
    stage_sink = _SceneDetectStageProgressSink(
        engine=engine,
        job_id=job_id,
        attempt_id=attempt_id,
        downstream=downstream,
    )
    collector = _Av1anProgressCollector(stage_sink, Av1anTtyProgressParser())
    raw = AV1AN_FIXTURE.read_bytes()

    collector.callback(
        ProcessOutputRecord(
            stream="stderr",
            data=raw,
            text=raw.decode("utf-8", errors="replace"),
        )
    )
    collector.flush()

    phases = [snapshot.phase for snapshot in downstream.snapshots]
    first_encode = phases.index(ProgressPhase.ENCODING)
    assert all(phase == ProgressPhase.SCENE_DETECTION for phase in phases[:first_encode])
    assert all(phase == ProgressPhase.ENCODING for phase in phases[first_encode:])
    encoding = [
        snapshot
        for snapshot in downstream.snapshots
        if snapshot.phase == ProgressPhase.ENCODING and snapshot.current is not None
    ]
    assert encoding[-1].current == 120
    assert encoding[-1].chunks_current == 1
    assert encoding[-1].chunks_total == 1

    with Session(engine) as session:
        job = session.get(Job, job_id)
        events = session.exec(select(JobEvent).order_by(col(JobEvent.id))).all()
    assert job is not None
    assert job.stage == JobStage.ENCODE
    assert [event.event_type for event in events] == [
        JobEventType.SCENE_DETECT_STARTED,
        JobEventType.SCENE_DETECT_COMPLETED,
        JobEventType.STAGE_STARTED,
    ]


def test_scene_detect_progress_with_scene_count_does_not_advance_to_encode(
    tmp_path: Path,
) -> None:
    engine, job_id, attempt_id = _claimed_scene_detect_job(tmp_path)
    downstream = _CaptureSink()
    sink = _SceneDetectStageProgressSink(
        engine=engine,
        job_id=job_id,
        attempt_id=attempt_id,
        downstream=downstream,
    )

    sink.publish(
        _progress(
            ProgressPhase.SCENE_DETECTION,
            message="scenes found: 142, chunks prepared: 317",
            observed_at=NOW + timedelta(seconds=4),
        )
    )

    with Session(engine) as session:
        job = session.get(Job, job_id)
        events = session.exec(select(JobEvent).order_by(col(JobEvent.id))).all()

    assert job is not None
    assert job.stage == JobStage.SCENE_DETECT
    assert job.status == JobStatus.ENCODING
    assert [event.event_type for event in events] == [JobEventType.SCENE_DETECT_STARTED]
    assert downstream.snapshots[-1].phase == ProgressPhase.SCENE_DETECTION


def test_scene_detect_progress_advances_to_encode_when_encoding_progress_starts(
    tmp_path: Path,
) -> None:
    engine, job_id, attempt_id = _claimed_scene_detect_job(tmp_path)
    sink = _SceneDetectStageProgressSink(
        engine=engine,
        job_id=job_id,
        attempt_id=attempt_id,
        downstream=_CaptureSink(),
    )

    sink.publish(
        _progress(
            ProgressPhase.SCENE_DETECTION,
            message="scenes found: 142, chunks prepared: 317",
            observed_at=NOW + timedelta(seconds=4),
        )
    )
    sink.publish(
        _progress(
            ProgressPhase.ENCODING,
            current=20,
            total=100,
            message=None,
            observed_at=NOW + timedelta(seconds=5),
        )
    )

    with Session(engine) as session:
        job = session.get(Job, job_id)
        events = session.exec(select(JobEvent).order_by(col(JobEvent.id))).all()

    assert job is not None
    assert job.stage == JobStage.ENCODE
    assert [event.event_type for event in events] == [
        JobEventType.SCENE_DETECT_STARTED,
        JobEventType.SCENE_DETECT_COMPLETED,
        JobEventType.STAGE_STARTED,
    ]
    assert [event.stage for event in events] == [
        JobStage.SCENE_DETECT,
        JobStage.SCENE_DETECT,
        JobStage.ENCODE,
    ]
    assert events[1].details_json == (
        '{"chunks_prepared":317,"duration_seconds":5.0,"scenes_found":142}'
    )


def test_scene_detect_transition_retries_after_a_transient_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, job_id, attempt_id = _claimed_scene_detect_job(tmp_path)
    downstream = _CaptureSink()
    sink = _SceneDetectStageProgressSink(
        engine=engine,
        job_id=job_id,
        attempt_id=attempt_id,
        downstream=downstream,
    )
    original = scheduler_workers.complete_scene_detect_stage
    calls = 0

    def fail_once(*args: Any, **kwargs: Any) -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient write failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(scheduler_workers, "complete_scene_detect_stage", fail_once)
    encoding = _progress(
        ProgressPhase.ENCODING,
        current=20,
        total=100,
        message=None,
        observed_at=NOW + timedelta(seconds=5),
    )

    with pytest.raises(RuntimeError, match="transient write failure"):
        sink.publish(encoding)
    sink.publish(encoding)

    with Session(engine) as session:
        job = session.get(Job, job_id)
    assert calls == 2
    assert job is not None
    assert job.stage == JobStage.ENCODE
    assert [snapshot.phase for snapshot in downstream.snapshots] == [ProgressPhase.ENCODING]


def test_legacy_encode_stage_without_scene_completion_is_restored(tmp_path: Path) -> None:
    engine, job_id, _attempt_id = _claimed_scene_detect_job(tmp_path)
    with Session(engine) as session, session.begin():
        job = session.get(Job, job_id)
        assert job is not None
        job.stage = JobStage.ENCODE
        job.status = JobStatus.QUEUED
        session.add(job)

    with Session(engine) as session, session.begin():
        job = session.get(Job, job_id)
        assert job is not None
        completed = _restore_missing_scene_detect_stage(session, job=job)

    with Session(engine) as session:
        job = session.get(Job, job_id)
    assert completed is False
    assert job is not None
    assert job.stage == JobStage.SCENE_DETECT


def test_late_scene_detect_progress_is_ignored_after_encoding_starts(tmp_path: Path) -> None:
    engine, job_id, attempt_id = _claimed_scene_detect_job(tmp_path)
    downstream = _CaptureSink()
    sink = _SceneDetectStageProgressSink(
        engine=engine,
        job_id=job_id,
        attempt_id=attempt_id,
        downstream=downstream,
    )

    sink.publish(
        _progress(
            ProgressPhase.SCENE_DETECTION,
            message="scenes found: 317",
            observed_at=NOW + timedelta(seconds=4),
        )
    )
    sink.publish(
        _progress(
            ProgressPhase.ENCODING,
            current=20,
            total=100,
            message=None,
            observed_at=NOW + timedelta(seconds=5),
        )
    )
    sink.publish(
        _progress(
            ProgressPhase.SCENE_DETECTION,
            message="scene scan 22480/31625 frames",
            observed_at=NOW + timedelta(seconds=6),
        )
    )

    assert [snapshot.phase for snapshot in downstream.snapshots] == [
        ProgressPhase.SCENE_DETECTION,
        ProgressPhase.ENCODING,
    ]


def test_scene_detect_progress_is_ignored_when_stage_already_completed(tmp_path: Path) -> None:
    engine, job_id, attempt_id = _claimed_scene_detect_job(tmp_path)
    downstream = _CaptureSink()
    sink = _SceneDetectStageProgressSink(
        engine=engine,
        job_id=job_id,
        attempt_id=attempt_id,
        downstream=downstream,
        scene_completed=True,
    )

    sink.publish(
        _progress(
            ProgressPhase.SCENE_DETECTION,
            message="scene scan 22480/31625 frames",
            observed_at=NOW + timedelta(seconds=6),
        )
    )
    sink.publish(
        _progress(
            ProgressPhase.ENCODING,
            current=25,
            total=100,
            message=None,
            observed_at=NOW + timedelta(seconds=7),
        )
    )

    assert [snapshot.phase for snapshot in downstream.snapshots] == [ProgressPhase.ENCODING]


def test_scene_detect_heartbeat_becomes_encoding_heartbeat_after_stage_completed(
    tmp_path: Path,
) -> None:
    engine, job_id, attempt_id = _claimed_scene_detect_job(tmp_path)
    downstream = _CaptureSink()
    sink = _SceneDetectStageProgressSink(
        engine=engine,
        job_id=job_id,
        attempt_id=attempt_id,
        downstream=downstream,
        scene_completed=True,
    )

    sink.publish(
        _progress(
            ProgressPhase.SCENE_DETECTION,
            message=None,
            observed_at=NOW + timedelta(seconds=6),
            source=ProgressSource.PROCESS_HEARTBEAT,
        )
    )

    assert len(downstream.snapshots) == 1
    assert downstream.snapshots[0].phase == ProgressPhase.ENCODING
    assert downstream.snapshots[0].source == ProgressSource.PROCESS_HEARTBEAT
    assert downstream.snapshots[0].message == "telemetry pending"


def _claimed_scene_detect_job(tmp_path: Path) -> tuple[Engine, int, int]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'avarch.adapters.sqlite.db'}")
    create_db_schema(engine)
    with Session(engine) as session:
        media_file = MediaFile(
            path=str(tmp_path / "movie.mkv"),
            size_bytes=1,
            mtime_ns=2,
            device_id=3,
            inode=4,
            fs_fingerprint="fingerprint",
            discovered_at=NOW,
            last_seen_at=NOW,
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
            status=JobStatus.QUEUED,
            stage=JobStage.SCENE_DETECT,
            created_at=NOW,
            updated_at=NOW,
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        attempt = claim_job_stage(session, job_id=job.id or 0, runner_id="runner", now=NOW)
        session.commit()
        return engine, job.id or 0, attempt.id or 0


def _progress(
    phase: ProgressPhase,
    *,
    current: int | None = None,
    total: int | None = None,
    message: str | None,
    observed_at: datetime,
    source: ProgressSource = ProgressSource.AV1AN_OUTPUT,
) -> ProgressSnapshot:
    return ProgressSnapshot(
        phase=phase,
        current=current,
        total=total,
        unit=ProgressUnit.FRAMES if current is not None or total is not None else None,
        rate_per_second=None,
        speed_ratio=None,
        source=source,
        message=message,
        phase_started_at=NOW,
        observed_at=observed_at,
        heartbeat_at=observed_at,
        advanced_at=observed_at if current is not None else None,
    )
