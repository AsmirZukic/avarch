from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session

from avarch.adapters.sqlite.db import create_db_engine, create_db_schema
from avarch.adapters.sqlite.models import Job, MediaFile, MediaFileStatus
from avarch.adapters.sqlite.queue import find_existing_queue_job
from avarch.domain.jobs import JobStage, JobStatus
from avarch.scheduler import build_queue_key


def test_queue_key_is_deterministic(tmp_path: Path) -> None:
    first = _queue_key(tmp_path)
    second = _queue_key(tmp_path)

    assert first == second


def test_queue_key_changes_with_source_fingerprint(tmp_path: Path) -> None:
    assert _queue_key(tmp_path, source_fs_fingerprint="one") != _queue_key(
        tmp_path,
        source_fs_fingerprint="two",
    )


def test_queue_key_changes_with_profile_hash(tmp_path: Path) -> None:
    assert _queue_key(tmp_path, profile_hash="one") != _queue_key(
        tmp_path,
        profile_hash="two",
    )


def test_queue_key_changes_with_probe_hash(tmp_path: Path) -> None:
    assert _queue_key(tmp_path, probe_hash="one") != _queue_key(tmp_path, probe_hash="two")


def test_queue_key_changes_with_vapoursynth_identity(tmp_path: Path) -> None:
    assert _queue_key(tmp_path, vapoursynth_identity_hash="one") != _queue_key(
        tmp_path,
        vapoursynth_identity_hash="two",
    )


def test_queue_key_changes_with_execution_identity(tmp_path: Path) -> None:
    assert _queue_key(tmp_path, execution_identity_hash="one") != _queue_key(
        tmp_path,
        execution_identity_hash="two",
    )


def test_null_probe_hash_is_distinct_from_real_probe_hash(tmp_path: Path) -> None:
    assert _queue_key(tmp_path, probe_hash=None) != _queue_key(tmp_path, probe_hash="")


def test_existing_completed_job_is_found_by_queue_key(tmp_path: Path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'avarch.adapters.sqlite.db'}")
    create_db_schema(engine)
    now = datetime.now(UTC)
    queue_key = _queue_key(tmp_path)

    with Session(engine) as session:
        media_file = MediaFile(
            path=str(tmp_path / "movie.mkv"),
            size_bytes=1,
            mtime_ns=2,
            device_id=3,
            inode=4,
            fs_fingerprint="fingerprint",
            discovered_at=now,
            last_seen_at=now,
            status=MediaFileStatus.PRESENT,
        )
        session.add(media_file)
        session.commit()
        session.refresh(media_file)
        session.add(
            Job(
                media_file_id=media_file.id or 0,
                profile_name="av1_1080p_sdr",
                profile_hash="profile-hash",
                source_fs_fingerprint="fingerprint",
                queue_key=queue_key,
                status=JobStatus.PROMOTED,
                stage=JobStage.ENCODE,
                created_at=now,
                updated_at=now,
                finished_at=now,
            )
        )
        session.commit()

        found = find_existing_queue_job(session, queue_key=queue_key)

    assert found is not None
    assert found.status == JobStatus.PROMOTED


def _queue_key(
    tmp_path: Path,
    *,
    source_fs_fingerprint: str = "fingerprint",
    profile_hash: str = "profile-hash",
    probe_hash: str | None = "probe-hash",
    vapoursynth_identity_hash: str = "vapoursynth-hash",
    execution_identity_hash: str = "execution-hash",
) -> str:
    return build_queue_key(
        media_path=tmp_path / "movie.mkv",
        source_fs_fingerprint=source_fs_fingerprint,
        profile_name="av1_1080p_sdr",
        profile_hash=profile_hash,
        probe_hash=probe_hash,
        vapoursynth_identity_hash=vapoursynth_identity_hash,
        execution_identity_hash=execution_identity_hash,
    )
