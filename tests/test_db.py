from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from avarch.db import create_db_engine, create_db_schema
from avarch.models.db import AppMeta, MediaFile, MediaFileStatus, ProbeResult


def test_create_db_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "avarch.db"
    engine = create_db_engine(f"sqlite:///{db_path}")

    create_db_schema(engine)

    with Session(engine) as session:
        session.add(AppMeta(key="schema_version", value="test"))
        session.commit()

        value = session.exec(select(AppMeta).where(AppMeta.key == "schema_version")).one()

    assert value.value == "test"


def test_insert_media_file(tmp_path: Path) -> None:
    db_path = tmp_path / "avarch.db"
    engine = create_db_engine(f"sqlite:///{db_path}")
    create_db_schema(engine)

    with Session(engine) as session:
        media_file = MediaFile(
            path="/media/example.mkv",
            size_bytes=123,
            mtime_ns=456,
            device_id=1,
            inode=2,
            fs_fingerprint="key",
            last_seen_at=datetime.now(UTC),
            status=MediaFileStatus.ADDED,
        )
        session.add(media_file)
        session.commit()

        stored = session.exec(select(MediaFile)).one()

    assert stored.path == "/media/example.mkv"


def test_insert_complete_media_file(tmp_path: Path) -> None:
    db_path = tmp_path / "avarch.db"
    engine = create_db_engine(f"sqlite:///{db_path}")
    create_db_schema(engine)
    now = datetime.now(UTC)

    with Session(engine) as session:
        session.add(
            MediaFile(
                path="/media/complete.mkv",
                size_bytes=123,
                mtime_ns=456,
                device_id=789,
                inode=101112,
                fs_fingerprint="abc123",
                discovered_at=now,
                last_seen_at=now,
                status=MediaFileStatus.PRESENT,
            )
        )
        session.commit()

        stored = session.exec(select(MediaFile)).one()

    assert stored.fs_fingerprint == "abc123"
    assert stored.status == MediaFileStatus.PRESENT


def test_media_file_path_is_unique(tmp_path: Path) -> None:
    db_path = tmp_path / "avarch.db"
    engine = create_db_engine(f"sqlite:///{db_path}")
    create_db_schema(engine)
    now = datetime.now(UTC)

    with Session(engine) as session:
        for fs_fingerprint in ("one", "two"):
            session.add(
                MediaFile(
                    path="/media/duplicate.mkv",
                    size_bytes=123,
                    mtime_ns=456,
                    device_id=789,
                    inode=101112,
                    fs_fingerprint=fs_fingerprint,
                    discovered_at=now,
                    last_seen_at=now,
                    status=MediaFileStatus.ADDED,
                )
            )

        with pytest.raises(IntegrityError):
            session.commit()


def test_media_file_status_round_trips(tmp_path: Path) -> None:
    db_path = tmp_path / "avarch.db"
    engine = create_db_engine(f"sqlite:///{db_path}")
    create_db_schema(engine)
    now = datetime.now(UTC)

    with Session(engine) as session:
        session.add(
            MediaFile(
                path="/media/status.mkv",
                size_bytes=123,
                mtime_ns=456,
                device_id=789,
                inode=101112,
                fs_fingerprint="status-key",
                discovered_at=now,
                last_seen_at=now,
                status=MediaFileStatus.CHANGED,
            )
        )
        session.commit()

        stored = session.exec(select(MediaFile)).one()

    assert stored.status == MediaFileStatus.CHANGED


def test_insert_probe_result_for_media_file(tmp_path: Path) -> None:
    db_path = tmp_path / "avarch.db"
    engine = create_db_engine(f"sqlite:///{db_path}")
    create_db_schema(engine)
    now = datetime.now(UTC)

    with Session(engine) as session:
        media_file = _media_file("/media/probed.mkv", now)
        session.add(media_file)
        session.commit()
        session.refresh(media_file)
        media_file_id = media_file.id or 0

        session.add(
            ProbeResult(
                media_file_id=media_file_id,
                ffprobe_json="{}",
                normalized_json="{}",
                probe_hash="hash",
                created_at=now,
            )
        )
        session.commit()

        stored = session.exec(select(ProbeResult)).one()

    assert stored.media_file_id == media_file_id


def test_probe_result_requires_existing_media_file(tmp_path: Path) -> None:
    db_path = tmp_path / "avarch.db"
    engine = create_db_engine(f"sqlite:///{db_path}")
    create_db_schema(engine)

    with Session(engine) as session:
        session.add(
            ProbeResult(
                media_file_id=999,
                ffprobe_json="{}",
                normalized_json="{}",
                probe_hash="hash",
                created_at=datetime.now(UTC),
            )
        )

        with pytest.raises(IntegrityError):
            session.commit()


def test_multiple_probe_results_can_exist_for_one_file(tmp_path: Path) -> None:
    db_path = tmp_path / "avarch.db"
    engine = create_db_engine(f"sqlite:///{db_path}")
    create_db_schema(engine)
    now = datetime.now(UTC)

    with Session(engine) as session:
        media_file = _media_file("/media/multiple.mkv", now)
        session.add(media_file)
        session.commit()
        session.refresh(media_file)

        for probe_hash in ("one", "two"):
            session.add(
                ProbeResult(
                    media_file_id=media_file.id or 0,
                    ffprobe_json="{}",
                    normalized_json="{}",
                    probe_hash=probe_hash,
                    created_at=now,
                )
            )
        session.commit()

        rows = session.exec(select(ProbeResult)).all()

    assert len(rows) == 2


def _media_file(path: str, now: datetime) -> MediaFile:
    return MediaFile(
        path=path,
        size_bytes=123,
        mtime_ns=456,
        device_id=789,
        inode=101112,
        fs_fingerprint=f"key:{path}",
        discovered_at=now,
        last_seen_at=now,
        status=MediaFileStatus.PRESENT,
    )
