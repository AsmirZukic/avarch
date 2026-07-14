from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlmodel import Session, col, select

from avarch.adapters.sqlite.db import create_db_engine, create_db_schema
from avarch.adapters.sqlite.job_transitions import claim_job_stage, interrupt_job_stage
from avarch.adapters.sqlite.models import (
    Job,
    JobAttemptProgress,
    MediaFile,
    MediaFileStatus,
    ProbeResult,
)
from avarch.adapters.sqlite.progress import ProgressPersistenceError, SqliteProgressStore
from avarch.domain.jobs import JobStage, JobStatus
from avarch.domain.progress import ProgressPhase, ProgressSnapshot, ProgressSource, ProgressUnit


def test_save_snapshot_inserts_first_attempt_progress(tmp_path: Path) -> None:
    engine, _job_id, attempt_id = _stored_running_attempt(tmp_path)
    observed = datetime(2026, 7, 1, 12, tzinfo=UTC)
    persisted = observed + timedelta(seconds=1)

    with Session(engine) as session:
        store = SqliteProgressStore(session)
        saved = store.save_snapshot(
            attempt_id=attempt_id,
            snapshot=_snapshot(observed_at=observed),
            persisted_at=persisted,
        )
        session.commit()

        row = session.get(JobAttemptProgress, attempt_id)

    assert saved is True
    assert row is not None
    assert row.phase == ProgressPhase.ENCODING
    assert row.current_value == 10.0
    assert row.total_value == 100.0
    assert row.unit == ProgressUnit.FRAMES
    assert row.created_at == persisted.replace(tzinfo=None)
    assert row.updated_at == persisted.replace(tzinfo=None)


def test_save_snapshot_updates_existing_progress_and_preserves_created_at(
    tmp_path: Path,
) -> None:
    engine, _job_id, attempt_id = _stored_running_attempt(tmp_path)
    first_observed = datetime(2026, 7, 1, 12, tzinfo=UTC)
    first_persisted = first_observed + timedelta(seconds=1)
    second_observed = first_observed + timedelta(seconds=5)
    second_persisted = second_observed + timedelta(seconds=1)

    with Session(engine) as session:
        store = SqliteProgressStore(session)
        store.save_snapshot(
            attempt_id=attempt_id,
            snapshot=_snapshot(observed_at=first_observed, current=10),
            persisted_at=first_persisted,
        )
        store.save_snapshot(
            attempt_id=attempt_id,
            snapshot=_snapshot(observed_at=second_observed, current=25, message="working"),
            persisted_at=second_persisted,
        )
        session.commit()

        row = session.get(JobAttemptProgress, attempt_id)

    assert row is not None
    assert row.current_value == 25.0
    assert row.message == "working"
    assert row.observed_at == second_observed.replace(tzinfo=None)
    assert row.created_at == first_persisted.replace(tzinfo=None)
    assert row.updated_at == second_persisted.replace(tzinfo=None)


def test_get_snapshot_returns_domain_snapshot(tmp_path: Path) -> None:
    engine, _job_id, attempt_id = _stored_running_attempt(tmp_path)
    observed = datetime(2026, 7, 1, 12, tzinfo=UTC)
    snapshot = _snapshot(observed_at=observed, current=30, total=None)

    with Session(engine) as session:
        store = SqliteProgressStore(session)
        store.save_snapshot(
            attempt_id=attempt_id,
            snapshot=snapshot,
            persisted_at=observed + timedelta(seconds=1),
        )
        session.commit()

        stored = store.get_snapshot(attempt_id=attempt_id)

    assert stored == ProgressSnapshot(
        phase=ProgressPhase.ENCODING,
        current=30.0,
        total=None,
        unit=ProgressUnit.FRAMES,
        rate_per_second=2.0,
        speed_ratio=1.5,
        source=ProgressSource.AV1AN_OUTPUT,
        message="encoding",
        phase_started_at=observed.replace(tzinfo=None),
        observed_at=observed.replace(tzinfo=None),
        heartbeat_at=observed.replace(tzinfo=None),
        advanced_at=observed.replace(tzinfo=None),
    )


def test_save_snapshot_rejects_nonexistent_attempt(tmp_path: Path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'avarch.adapters.sqlite.db'}")
    create_db_schema(engine)
    observed = datetime(2026, 7, 1, 12, tzinfo=UTC)

    with Session(engine) as session:
        store = SqliteProgressStore(session)
        with pytest.raises(ProgressPersistenceError, match="Attempt not found"):
            store.save_snapshot(
                attempt_id=999,
                snapshot=_snapshot(observed_at=observed),
                persisted_at=observed,
            )


def test_save_snapshot_ignores_older_out_of_order_snapshot(tmp_path: Path) -> None:
    engine, _job_id, attempt_id = _stored_running_attempt(tmp_path)
    later = datetime(2026, 7, 1, 12, 0, 10, tzinfo=UTC)
    older = later - timedelta(seconds=5)

    with Session(engine) as session:
        store = SqliteProgressStore(session)
        store.save_snapshot(
            attempt_id=attempt_id,
            snapshot=_snapshot(observed_at=later, current=40),
            persisted_at=later,
        )
        saved = store.save_snapshot(
            attempt_id=attempt_id,
            snapshot=_snapshot(observed_at=older, current=20),
            persisted_at=later + timedelta(seconds=1),
        )
        session.commit()

        row = session.get(JobAttemptProgress, attempt_id)

    assert saved is False
    assert row is not None
    assert row.current_value == 40.0
    assert row.observed_at == later.replace(tzinfo=None)


def test_save_snapshot_allows_phase_transition_with_lower_current(tmp_path: Path) -> None:
    engine, _job_id, attempt_id = _stored_running_attempt(tmp_path)
    first = datetime(2026, 7, 1, 12, tzinfo=UTC)
    second = first + timedelta(seconds=5)

    with Session(engine) as session:
        store = SqliteProgressStore(session)
        store.save_snapshot(
            attempt_id=attempt_id,
            snapshot=_snapshot(observed_at=first, current=100, phase=ProgressPhase.ENCODING),
            persisted_at=first,
        )
        saved = store.save_snapshot(
            attempt_id=attempt_id,
            snapshot=_snapshot(
                observed_at=second,
                current=0,
                total=None,
                phase=ProgressPhase.MUXING,
                unit=ProgressUnit.UNKNOWN,
                source=ProgressSource.SCHEDULER,
            ),
            persisted_at=second,
        )
        session.commit()

        row = session.get(JobAttemptProgress, attempt_id)

    assert saved is True
    assert row is not None
    assert row.phase == ProgressPhase.MUXING
    assert row.current_value == 0.0


def test_save_snapshot_ignores_same_phase_current_reset_for_same_attempt(
    tmp_path: Path,
) -> None:
    engine, _job_id, attempt_id = _stored_running_attempt(tmp_path)
    first = datetime(2026, 7, 1, 12, tzinfo=UTC)
    second = first + timedelta(seconds=5)

    with Session(engine) as session:
        store = SqliteProgressStore(session)
        store.save_snapshot(
            attempt_id=attempt_id,
            snapshot=_snapshot(observed_at=first, current=80),
            persisted_at=first,
        )
        saved = store.save_snapshot(
            attempt_id=attempt_id,
            snapshot=_snapshot(observed_at=second, current=10),
            persisted_at=second,
        )
        session.commit()

        row = session.get(JobAttemptProgress, attempt_id)

    assert saved is False
    assert row is not None
    assert row.current_value == 80.0


def test_retry_attempt_can_start_with_lower_current_without_overwriting_previous_attempt(
    tmp_path: Path,
) -> None:
    engine, job_id, first_attempt_id = _stored_running_attempt(tmp_path)
    first = datetime(2026, 7, 1, 12, tzinfo=UTC)
    second = first + timedelta(seconds=5)

    with Session(engine) as session:
        store = SqliteProgressStore(session)
        store.save_snapshot(
            attempt_id=first_attempt_id,
            snapshot=_snapshot(observed_at=first, current=80),
            persisted_at=first,
        )
        interrupt_job_stage(
            session,
            job_id=job_id,
            attempt_id=first_attempt_id,
            now=second - timedelta(seconds=1),
        )
        second_attempt = claim_job_stage(
            session,
            job_id=job_id,
            runner_id="runner-2",
            now=second,
        )
        assert second_attempt.id is not None
        second_attempt_id = second_attempt.id
        store.save_snapshot(
            attempt_id=second_attempt_id,
            snapshot=_snapshot(observed_at=second, current=5),
            persisted_at=second,
        )
        session.commit()

        progress = list(
            session.exec(
                select(JobAttemptProgress).order_by(col(JobAttemptProgress.attempt_id))
            ).all()
        )

    assert [(row.attempt_id, row.current_value) for row in progress] == [
        (first_attempt_id, 80.0),
        (second_attempt_id, 5.0),
    ]


def test_save_snapshot_persists_unknown_total(tmp_path: Path) -> None:
    engine, _job_id, attempt_id = _stored_running_attempt(tmp_path)
    observed = datetime(2026, 7, 1, 12, tzinfo=UTC)

    with Session(engine) as session:
        store = SqliteProgressStore(session)
        store.save_snapshot(
            attempt_id=attempt_id,
            snapshot=_snapshot(observed_at=observed, current=12, total=None),
            persisted_at=observed,
        )
        session.commit()

        row = session.get(JobAttemptProgress, attempt_id)

    assert row is not None
    assert row.current_value == 12.0
    assert row.total_value is None
    assert row.unit == ProgressUnit.FRAMES


def test_save_snapshot_does_not_modify_job_state_version(tmp_path: Path) -> None:
    engine, job_id, attempt_id = _stored_running_attempt(tmp_path)
    observed = datetime(2026, 7, 1, 12, tzinfo=UTC)

    with Session(engine) as session:
        before = session.get(Job, job_id)
        assert before is not None
        assert before.state_version == 2

        store = SqliteProgressStore(session)
        store.save_snapshot(
            attempt_id=attempt_id,
            snapshot=_snapshot(observed_at=observed),
            persisted_at=observed,
        )
        session.commit()

    with Session(engine) as session:
        after = session.get(Job, job_id)

    assert after is not None
    assert after.state_version == 2


def test_get_snapshot_returns_none_when_attempt_has_no_progress(tmp_path: Path) -> None:
    engine, _job_id, attempt_id = _stored_running_attempt(tmp_path)

    with Session(engine) as session:
        store = SqliteProgressStore(session)

        assert store.get_snapshot(attempt_id=attempt_id) is None


def _stored_running_attempt(tmp_path: Path) -> tuple[Engine, int, int]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'avarch.adapters.sqlite.db'}")
    create_db_schema(engine)
    now = datetime(2026, 7, 1, 11, tzinfo=UTC)

    with Session(engine) as session:
        media_file = MediaFile(
            path="/media/progress.mkv",
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
        session.commit()
        session.refresh(media_file)
        media_file_id = media_file.id
        assert media_file_id is not None

        probe = ProbeResult(
            media_file_id=media_file_id,
            ffprobe_json="{}",
            normalized_json="{}",
            probe_hash="probe-hash",
            source_fs_fingerprint=media_file.fs_fingerprint,
            created_at=now,
        )
        session.add(probe)
        session.commit()
        session.refresh(probe)
        probe_id = probe.id
        assert probe_id is not None

        job = Job(
            media_file_id=media_file_id,
            profile_name="av1_1080p_sdr",
            profile_hash="profile-hash",
            source_fs_fingerprint=media_file.fs_fingerprint,
            queue_key="queue-progress",
            probe_result_id=probe_id,
            probe_hash=probe.probe_hash,
            status=JobStatus.QUEUED,
            stage=JobStage.ENCODE,
            priority=0,
            attempts=0,
            created_at=now,
            updated_at=now,
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.id
        assert job_id is not None

        attempt = claim_job_stage(
            session,
            job_id=job_id,
            runner_id="runner",
            now=now,
        )
        session.commit()
        attempt_id = attempt.id
        assert attempt_id is not None

    return engine, job_id, attempt_id


def _snapshot(
    *,
    observed_at: datetime,
    current: float | None = 10,
    total: float | None = 100,
    phase: ProgressPhase = ProgressPhase.ENCODING,
    unit: ProgressUnit | None = ProgressUnit.FRAMES,
    source: ProgressSource = ProgressSource.AV1AN_OUTPUT,
    message: str | None = "encoding",
) -> ProgressSnapshot:
    return ProgressSnapshot(
        phase=phase,
        current=current,
        total=total,
        unit=unit,
        rate_per_second=2.0,
        speed_ratio=1.5,
        source=source,
        message=message,
        phase_started_at=observed_at,
        observed_at=observed_at,
        heartbeat_at=observed_at,
        advanced_at=observed_at if current is not None else None,
    )
