from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from avarch.adapters.sqlite.db import create_db_engine, create_db_schema
from avarch.adapters.sqlite.models import (
    Job,
    JobAttempt,
    JobAttemptProgress,
    MediaFile,
    MediaFileStatus,
    ProbeResult,
)
from avarch.domain.jobs import AttemptStatus, JobStage, JobStatus, ResourceClass
from avarch.domain.progress import ProgressPhase, ProgressSource, ProgressUnit


def test_attempt_progress_round_trips_nullable_snapshot_fields(tmp_path: Path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'avarch.adapters.sqlite.db'}")
    create_db_schema(engine)
    now = datetime(2026, 7, 1, tzinfo=UTC)

    with Session(engine) as session:
        attempt_id = _insert_attempt(session, now=now)
        session.add(
            JobAttemptProgress(
                attempt_id=attempt_id,
                phase=ProgressPhase.PREPARING,
                current_value=None,
                total_value=None,
                unit=None,
                rate_per_second=None,
                speed_ratio=None,
                source=ProgressSource.SCHEDULER,
                message=None,
                phase_started_at=now,
                observed_at=now,
                heartbeat_at=now,
                advanced_at=None,
                created_at=now,
                updated_at=now,
            )
        )
        session.commit()

        stored = session.exec(select(JobAttemptProgress)).one()

    assert stored.attempt_id == attempt_id
    assert stored.phase == ProgressPhase.PREPARING
    assert stored.current_value is None
    assert stored.total_value is None
    assert stored.unit is None
    assert stored.rate_per_second is None
    assert stored.speed_ratio is None
    assert stored.source == ProgressSource.SCHEDULER
    assert stored.message is None
    assert stored.advanced_at is None


def test_attempt_progress_requires_existing_attempt(tmp_path: Path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'avarch.adapters.sqlite.db'}")
    create_db_schema(engine)
    now = datetime(2026, 7, 1, tzinfo=UTC)

    with Session(engine) as session:
        session.add(_progress(attempt_id=999, now=now))

        with pytest.raises(IntegrityError):
            session.commit()


def test_attempt_progress_has_one_row_per_attempt(tmp_path: Path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'avarch.adapters.sqlite.db'}")
    create_db_schema(engine)
    now = datetime(2026, 7, 1, tzinfo=UTC)

    with Session(engine) as session:
        attempt_id = _insert_attempt(session, now=now)
        session.add(_progress(attempt_id=attempt_id, now=now))
        session.commit()

        session.add(_progress(attempt_id=attempt_id, now=now, message="duplicate"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_attempt_progress_prevents_deleting_attempt_while_present(
    tmp_path: Path,
) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'avarch.adapters.sqlite.db'}")
    create_db_schema(engine)
    now = datetime(2026, 7, 1, tzinfo=UTC)

    with Session(engine) as session:
        attempt_id = _insert_attempt(session, now=now)
        session.add(_progress(attempt_id=attempt_id, now=now))
        session.commit()

        attempt = session.get(JobAttempt, attempt_id)
        assert attempt is not None
        session.delete(attempt)
        with pytest.raises(IntegrityError):
            session.commit()


def _insert_attempt(session: Session, *, now: datetime) -> int:
    media_file = MediaFile(
        path=f"/media/{now.timestamp()}.mkv",
        size_bytes=123,
        mtime_ns=456,
        device_id=789,
        inode=101112,
        fs_fingerprint=f"key:{now.timestamp()}",
        discovered_at=now,
        last_seen_at=now,
        status=MediaFileStatus.PRESENT,
    )
    session.add(media_file)
    session.commit()
    session.refresh(media_file)
    media_id = media_file.id
    assert media_id is not None

    probe = ProbeResult(
        media_file_id=media_id,
        ffprobe_json="{}",
        normalized_json="{}",
        probe_hash=f"probe:{media_id}",
        source_fs_fingerprint=media_file.fs_fingerprint,
        created_at=now,
    )
    session.add(probe)
    session.commit()
    session.refresh(probe)
    probe_id = probe.id
    assert probe_id is not None

    job = Job(
        media_file_id=media_id,
        profile_name="av1_1080p_sdr",
        profile_hash="profile-hash",
        source_fs_fingerprint=media_file.fs_fingerprint,
        queue_key=f"queue:{media_id}",
        probe_result_id=probe_id,
        probe_hash=probe.probe_hash,
        status=JobStatus.ENCODING,
        stage=JobStage.ENCODE,
        priority=0,
        attempts=1,
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    session.commit()
    session.refresh(job)
    job_id = job.id
    assert job_id is not None

    attempt = JobAttempt(
        job_id=job_id,
        attempt_number=1,
        stage=JobStage.ENCODE,
        resource_class=ResourceClass.HEAVY_AV1AN,
        status=AttemptStatus.RUNNING,
        runner_id="runner",
        started_at=now,
    )
    session.add(attempt)
    session.commit()
    session.refresh(attempt)
    attempt_id = attempt.id
    assert attempt_id is not None
    return attempt_id


def _progress(
    *,
    attempt_id: int,
    now: datetime,
    message: str | None = "encoding",
) -> JobAttemptProgress:
    return JobAttemptProgress(
        attempt_id=attempt_id,
        phase=ProgressPhase.ENCODING,
        current_value=12,
        total_value=24,
        unit=ProgressUnit.FRAMES,
        rate_per_second=6.0,
        speed_ratio=1.2,
        source=ProgressSource.AV1AN_OUTPUT,
        message=message,
        phase_started_at=now,
        observed_at=now,
        heartbeat_at=now,
        advanced_at=now,
        created_at=now,
        updated_at=now,
    )
