from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session, select

from avarch.adapters.sqlite.db import create_db_engine, create_db_schema
from avarch.adapters.sqlite.models import Job, MediaFile, MediaFileStatus
from avarch.adapters.sqlite.probes import store_probe_result
from avarch.config import AppConfig
from avarch.domain.jobs import JobStage, JobStatus
from avarch.probe import normalize_probe
from avarch.scheduler import enqueue_inventory
from tests.probe_fixtures import sdr_probe_payload


def test_enqueue_creates_job_for_present_file(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)

    with Session(engine) as session:
        _add_media_file(session, tmp_path / "movie.mkv", now)
        summary = enqueue_inventory(
            session,
            config=_config(),
            profile_name="av1_1080p_sdr",
            priority=0,
            now=now,
        )
        session.commit()

        job = session.exec(select(Job)).one()

    assert summary.created == 1
    assert job.status == JobStatus.QUEUED


def test_enqueue_excludes_missing_file(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)

    with Session(engine) as session:
        _add_media_file(session, tmp_path / "missing.mkv", now, status=MediaFileStatus.MISSING)
        summary = enqueue_inventory(
            session,
            config=_config(),
            profile_name="av1_1080p_sdr",
            priority=0,
            now=now,
        )

    assert summary.created == 0
    assert summary.missing_skipped == 1


def test_enqueue_uses_probe_stage_without_fresh_probe(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)

    with Session(engine) as session:
        _add_media_file(session, tmp_path / "movie.mkv", now)
        enqueue_inventory(
            session,
            config=_config(),
            profile_name="av1_1080p_sdr",
            priority=0,
            now=now,
        )
        session.commit()
        job = session.exec(select(Job)).one()

    assert job.stage == JobStage.PROBE


def test_enqueue_uses_plan_stage_with_fresh_probe(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)

    with Session(engine) as session:
        media_file = _add_media_file(session, tmp_path / "movie.mkv", now)
        _add_probe(session, media_file, now)
        enqueue_inventory(
            session,
            config=_config(),
            profile_name="av1_1080p_sdr",
            priority=0,
            now=now,
        )
        session.commit()
        job = session.exec(select(Job)).one()

    assert job.stage == JobStage.PLAN
    assert job.probe_hash is not None


def test_enqueue_stores_source_fingerprint(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)

    with Session(engine) as session:
        media_file = _add_media_file(session, tmp_path / "movie.mkv", now)
        fs_fingerprint = media_file.fs_fingerprint
        enqueue_inventory(
            session,
            config=_config(),
            profile_name="av1_1080p_sdr",
            priority=0,
            now=now,
        )
        session.commit()
        job = session.exec(select(Job)).one()

    assert job.source_fs_fingerprint == fs_fingerprint


def test_enqueue_stores_effective_profile_hash(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)

    with Session(engine) as session:
        _add_media_file(session, tmp_path / "movie.mkv", now)
        enqueue_inventory(
            session,
            config=_config(),
            profile_name="av1_1080p_sdr",
            priority=0,
            now=now,
        )
        session.commit()
        job = session.exec(select(Job)).one()

    assert len(job.profile_hash) == 64


def test_enqueue_does_not_duplicate_existing_job(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)

    with Session(engine) as session:
        _add_media_file(session, tmp_path / "movie.mkv", now)
        first = enqueue_inventory(
            session,
            config=_config(),
            profile_name="av1_1080p_sdr",
            priority=0,
            now=now,
        )
        second = enqueue_inventory(
            session,
            config=_config(),
            profile_name="av1_1080p_sdr",
            priority=0,
            now=now,
        )
        session.commit()
        jobs = session.exec(select(Job)).all()

    assert first.created == 1
    assert second.existing == 1
    assert len(jobs) == 1


def test_enqueue_allows_new_job_when_probe_hash_changes(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)

    with Session(engine) as session:
        media_file = _add_media_file(session, tmp_path / "movie.mkv", now)
        enqueue_inventory(
            session,
            config=_config(),
            profile_name="av1_1080p_sdr",
            priority=0,
            now=now,
        )
        _add_probe(session, media_file, now)
        enqueue_inventory(
            session,
            config=_config(),
            profile_name="av1_1080p_sdr",
            priority=0,
            now=now,
        )
        session.commit()

        jobs = session.exec(select(Job)).all()

    assert len(jobs) == 2


def test_enqueue_allows_new_job_when_profile_changes(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)
    profile_dir = tmp_path / "profiles"
    _write_profile(profile_dir, name="custom_1920", max_width=1920)
    _write_profile(profile_dir, name="custom_1280", max_width=1280)
    config = _config(profile_dir=profile_dir)

    with Session(engine) as session:
        _add_media_file(session, tmp_path / "movie.mkv", now)
        enqueue_inventory(
            session,
            config=config,
            profile_name="custom_1920",
            priority=0,
            now=now,
        )
        enqueue_inventory(
            session,
            config=config,
            profile_name="custom_1280",
            priority=0,
            now=now,
        )
        session.commit()

        jobs = session.exec(select(Job)).all()

    assert len(jobs) == 2


def _engine(tmp_path: Path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'avarch.adapters.sqlite.db'}")
    create_db_schema(engine)
    return engine


def _add_media_file(
    session: Session,
    path: Path,
    now: datetime,
    *,
    status: MediaFileStatus = MediaFileStatus.PRESENT,
) -> MediaFile:
    path.write_bytes(b"media")
    media_file = MediaFile(
        path=str(path.resolve()),
        size_bytes=1,
        mtime_ns=2,
        device_id=3,
        inode=4,
        fs_fingerprint=f"fingerprint:{path.name}",
        discovered_at=now,
        last_seen_at=now,
        status=status,
    )
    session.add(media_file)
    session.commit()
    session.refresh(media_file)
    return media_file


def _add_probe(session: Session, media_file: MediaFile, now: datetime) -> None:
    raw_probe = sdr_probe_payload()
    normalized = normalize_probe(raw_probe)
    store_probe_result(
        session,
        media_file=media_file,
        raw_probe=raw_probe,
        normalized_probe=normalized,
        created_at=now,
    )
    session.commit()
    session.refresh(media_file)


def _config(*, profile_dir: Path | None = None) -> AppConfig:
    if profile_dir is None:
        return AppConfig()
    return AppConfig.model_validate({"profile_registry": {"search_paths": [profile_dir]}})


def _write_profile(profile_dir: Path, *, name: str, max_width: int) -> None:
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / f"{name}.toml").write_text(
        f"""
schema_version = 1
name = "{name}"

backend = "av1an"
container = "mkv"

[match]
video_codec_not = ["av1"]

[video]
max_width = {max_width}
hdr_to_sdr = true
source = "vapoursynth"

[av1an]
encoder = "svt-av1"
workers = 6
video_args = "--preset 6 --crf 28"

[audio]
codec = "libopus"
bitrate = "128k"
channels = 2
languages = ["eng"]

[subtitles]
languages = ["eng"]
keep_forced = true
""".strip(),
        encoding="utf-8",
    )
