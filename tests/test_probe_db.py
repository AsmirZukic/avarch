from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine
from sqlmodel import Session

from avarch.db import create_db_engine, create_db_schema
from avarch.models.db import MediaFile, MediaFileStatus, ProbeResult
from avarch.models.probe import NormalizedProbe
from avarch.probe import get_latest_probe_result, store_probe_result


def test_store_probe_result_persists_raw_json(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media_file = _insert_media_file(engine)

    with Session(engine) as session:
        stored_media_file = session.get_one(MediaFile, media_file.id)
        result = store_probe_result(
            session,
            media_file=stored_media_file,
            raw_probe={"b": 1, "a": 2},
            normalized_probe=NormalizedProbe(),
            created_at=_now(),
        )
        session.commit()
        session.refresh(result)

        stored = session.get_one(ProbeResult, result.id)

    assert stored.ffprobe_json == '{"a":2,"b":1}'


def test_store_probe_result_persists_normalized_json(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media_file = _insert_media_file(engine)

    with Session(engine) as session:
        stored_media_file = session.get_one(MediaFile, media_file.id)
        result = store_probe_result(
            session,
            media_file=stored_media_file,
            raw_probe={},
            normalized_probe=NormalizedProbe(container="matroska,webm"),
            created_at=_now(),
        )
        session.commit()
        session.refresh(result)

    assert '"container":"matroska,webm"' in result.normalized_json


def test_store_probe_result_persists_hash(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media_file = _insert_media_file(engine)

    with Session(engine) as session:
        stored_media_file = session.get_one(MediaFile, media_file.id)
        result = store_probe_result(
            session,
            media_file=stored_media_file,
            raw_probe={},
            normalized_probe=NormalizedProbe(),
            created_at=_now(),
        )
        session.commit()
        session.refresh(result)

    assert len(result.probe_hash) == 64


def test_get_latest_probe_result_returns_newest(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media_file = _insert_media_file(engine)

    with Session(engine) as session:
        stored_media_file = session.get_one(MediaFile, media_file.id)
        older = store_probe_result(
            session,
            media_file=stored_media_file,
            raw_probe={},
            normalized_probe=NormalizedProbe(duration_seconds=1.0),
            created_at=_now(),
        )
        newer = store_probe_result(
            session,
            media_file=stored_media_file,
            raw_probe={},
            normalized_probe=NormalizedProbe(duration_seconds=2.0),
            created_at=_now() + timedelta(seconds=1),
        )
        session.commit()
        session.refresh(older)
        session.refresh(newer)

        latest = get_latest_probe_result(session, media_file_id=stored_media_file.id or 0)

    assert latest is not None
    assert latest.id == newer.id


def test_get_latest_probe_result_returns_none_without_results(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media_file = _insert_media_file(engine)

    with Session(engine) as session:
        latest = get_latest_probe_result(session, media_file_id=media_file.id or 0)

    assert latest is None


def _engine(tmp_path: Path) -> Engine:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'avarch.db'}")
    create_db_schema(engine)
    return engine


def _insert_media_file(engine: Engine) -> MediaFile:
    media_file = MediaFile(
        path="/media/movie.mkv",
        size_bytes=1,
        mtime_ns=2,
        device_id=3,
        inode=4,
        content_key="key",
        discovered_at=_now(),
        last_seen_at=_now(),
        status=MediaFileStatus.PRESENT,
    )
    with Session(engine) as session:
        session.add(media_file)
        session.commit()
        session.refresh(media_file)
    return media_file


def _now() -> datetime:
    return datetime(2026, 6, 14, tzinfo=UTC)
