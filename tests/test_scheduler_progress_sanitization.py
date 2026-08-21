from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session

from avarch.adapters.scheduler_workers import (
    _sanitize_progress_message,  # pyright: ignore[reportPrivateUsage]
    _save_attempt_progress,  # pyright: ignore[reportPrivateUsage]
)
from avarch.adapters.sqlite.db import create_db_engine, create_db_schema
from avarch.adapters.sqlite.models import Job, JobAttempt, MediaFile, MediaFileStatus
from avarch.adapters.sqlite.progress import SqliteProgressStore
from avarch.domain.jobs import AttemptStatus, JobStage, JobStatus, ResourceClass
from avarch.domain.progress import ProgressPhase, ProgressSnapshot, ProgressSource


def test_progress_message_sanitizer_bounds_ansi_and_control_characters() -> None:
    message = "\x1b[31msecret\x1b[0m\0\n" + ("x" * 600)

    sanitized = _sanitize_progress_message(message)

    assert sanitized is not None
    assert "\x1b" not in sanitized
    assert "\0" not in sanitized
    assert "\n" not in sanitized
    assert sanitized.startswith("secret")
    assert len(sanitized) <= 500


def test_external_progress_messages_are_sanitized_before_persistence(tmp_path: Path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'progress.db'}")
    create_db_schema(engine)
    now = datetime(2026, 7, 1, 12, tzinfo=UTC)
    with Session(engine) as session, session.begin():
        media_file = MediaFile(
            path=str(tmp_path / "movie.mkv"),
            size_bytes=100,
            mtime_ns=1,
            device_id=2,
            inode=3,
            fs_fingerprint="fingerprint",
            status=MediaFileStatus.PRESENT,
        )
        session.add(media_file)
        session.flush()
        media_file_id = media_file.id
        assert media_file_id is not None
        job = Job(
            media_file_id=media_file_id,
            profile_name="profile",
            profile_hash="profile-hash",
            source_fs_fingerprint=media_file.fs_fingerprint,
            queue_key="queue",
            status=JobStatus.ENCODING,
            stage=JobStage.ENCODE,
            created_at=now,
            updated_at=now,
        )
        session.add(job)
        session.flush()
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
        session.flush()
        attempt_id = attempt.id
        assert attempt_id is not None

    _save_attempt_progress(
        engine,
        attempt_id,
        ProgressSnapshot(
            phase=ProgressPhase.ENCODING,
            current=None,
            total=None,
            unit=None,
            rate_per_second=None,
            speed_ratio=None,
            source=ProgressSource.AV1AN_OUTPUT,
            message="\x1b[2K0/1 chunks\r\n" + ("x" * 600),
            phase_started_at=now,
            observed_at=now,
            heartbeat_at=now,
            advanced_at=None,
        ),
    )

    with Session(engine) as session:
        snapshot = SqliteProgressStore(session).get_snapshot(attempt_id=attempt_id)

    assert snapshot is not None
    assert snapshot.message is not None
    assert snapshot.message.startswith("0/1 chunks")
    assert "\x1b" not in snapshot.message
    assert "\r" not in snapshot.message
    assert len(snapshot.message) <= 500
