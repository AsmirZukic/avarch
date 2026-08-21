from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine
from sqlmodel import Session

from avarch.adapters.sqlite.db import create_db_engine, create_db_schema
from avarch.adapters.sqlite.models import MediaFile, MediaFileStatus, ProbeResult
from avarch.adapters.sqlite.probes import get_canonical_probe_result, store_probe_result
from avarch.models.probe import NormalizedProbe


def test_store_probe_result_persists_normalized_json(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media_file = _insert_media_file(engine)

    with Session(engine) as session:
        stored_media_file = session.get_one(MediaFile, media_file.id)
        result = store_probe_result(
            session,
            media_file=stored_media_file,
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
            normalized_probe=NormalizedProbe(),
            created_at=_now(),
        )
        session.commit()
        session.refresh(result)

    assert len(result.probe_hash) == 64


def test_successful_probe_stores_source_fs_fingerprint(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media_file = _insert_media_file(engine)

    with Session(engine) as session:
        stored_media_file = session.get_one(MediaFile, media_file.id)
        result = store_probe_result(
            session,
            media_file=stored_media_file,
            normalized_probe=NormalizedProbe(),
            created_at=_now(),
        )
        session.commit()
        session.refresh(result)

    assert result.source_fs_fingerprint == "key"


def test_successful_probe_sets_latest_probe_id(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media_file = _insert_media_file(engine)

    with Session(engine) as session:
        stored_media_file = session.get_one(MediaFile, media_file.id)
        result = store_probe_result(
            session,
            media_file=stored_media_file,
            normalized_probe=NormalizedProbe(),
            created_at=_now(),
        )
        session.commit()
        session.refresh(stored_media_file)
        session.refresh(result)

    assert stored_media_file.latest_probe_id == result.id


def test_second_successful_probe_replaces_latest_probe_pointer(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media_file = _insert_media_file(engine)

    with Session(engine) as session:
        stored_media_file = session.get_one(MediaFile, media_file.id)
        first = store_probe_result(
            session,
            media_file=stored_media_file,
            normalized_probe=NormalizedProbe(duration_seconds=1.0),
            created_at=_now(),
        )
        second = store_probe_result(
            session,
            media_file=stored_media_file,
            normalized_probe=NormalizedProbe(duration_seconds=2.0),
            created_at=_now(),
        )
        session.commit()
        session.refresh(stored_media_file)
        session.refresh(first)
        session.refresh(second)

        canonical = get_canonical_probe_result(session, stored_media_file)

    assert stored_media_file.latest_probe_id == second.id
    assert canonical is not None
    assert canonical.id == second.id
    assert first.id != second.id


def test_previous_probe_result_remains_stored(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media_file = _insert_media_file(engine)

    with Session(engine) as session:
        stored_media_file = session.get_one(MediaFile, media_file.id)
        first = store_probe_result(
            session,
            media_file=stored_media_file,
            normalized_probe=NormalizedProbe(duration_seconds=1.0),
            created_at=_now(),
        )
        second = store_probe_result(
            session,
            media_file=stored_media_file,
            normalized_probe=NormalizedProbe(duration_seconds=2.0),
            created_at=_now(),
        )
        session.commit()
        session.refresh(first)
        session.refresh(second)

        assert session.get(ProbeResult, first.id) is not None
        assert session.get(ProbeResult, second.id) is not None


def test_canonical_probe_returns_none_without_pointer(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media_file = _insert_media_file(engine)

    with Session(engine) as session:
        stored_media_file = session.get_one(MediaFile, media_file.id)
        canonical = get_canonical_probe_result(session, stored_media_file)

    assert canonical is None


def test_canonical_probe_rejects_stale_source_fingerprint(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media_file = _insert_media_file(engine)

    with Session(engine) as session:
        stored_media_file = session.get_one(MediaFile, media_file.id)
        store_probe_result(
            session,
            media_file=stored_media_file,
            normalized_probe=NormalizedProbe(),
            created_at=_now(),
        )
        stored_media_file.fs_fingerprint = "changed"
        session.add(stored_media_file)
        session.commit()
        session.refresh(stored_media_file)

        canonical = get_canonical_probe_result(session, stored_media_file)

    assert canonical is None


def test_canonical_probe_rejects_pointer_to_another_media_file(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    first_media_file = _insert_media_file(engine, path="/media/one.mkv", fingerprint="one")
    second_media_file = _insert_media_file(engine, path="/media/two.mkv", fingerprint="two")

    with Session(engine) as session:
        first = session.get_one(MediaFile, first_media_file.id)
        second = session.get_one(MediaFile, second_media_file.id)
        probe_result = store_probe_result(
            session,
            media_file=second,
            normalized_probe=NormalizedProbe(),
            created_at=_now(),
        )
        session.flush()
        first.latest_probe_id = probe_result.id
        first.fs_fingerprint = second.fs_fingerprint
        session.add(first)
        session.commit()
        session.refresh(first)

        canonical = get_canonical_probe_result(session, first)

    assert canonical is None


def _engine(tmp_path: Path) -> Engine:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'avarch.db'}")
    create_db_schema(engine)
    return engine


def _insert_media_file(
    engine: Engine,
    *,
    path: str = "/media/movie.mkv",
    fingerprint: str = "key",
) -> MediaFile:
    media_file = MediaFile(
        path=path,
        size_bytes=1,
        mtime_ns=2,
        device_id=3,
        inode=4,
        fs_fingerprint=fingerprint,
        status=MediaFileStatus.PRESENT,
    )
    with Session(engine) as session:
        session.add(media_file)
        session.commit()
        session.refresh(media_file)
    return media_file


def _now() -> datetime:
    return datetime(2026, 6, 14, tzinfo=UTC)
