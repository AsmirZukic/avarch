from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from avarch.adapters.sqlite.db import create_db_engine, create_db_schema
from avarch.adapters.sqlite.models import AppMeta, MediaFile, MediaFileStatus, ProbeResult


def test_create_db_schema(tmp_path: Path) -> None:
    db_path = tmp_path / "avarch.adapters.sqlite.db"
    engine = create_db_engine(f"sqlite:///{db_path}")

    create_db_schema(engine)

    with Session(engine) as session:
        session.add(AppMeta(key="example", value="test"))
        session.commit()

        value = session.exec(select(AppMeta).where(AppMeta.key == "example")).one()

    assert value.value == "test"


def test_sqlite_foreign_keys_are_enabled(tmp_path: Path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'avarch.adapters.sqlite.db'}")

    with engine.connect() as connection:
        enabled = connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one()

    assert enabled == 1


def test_sqlite_busy_timeout_is_configured(tmp_path: Path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'avarch.adapters.sqlite.db'}")

    with engine.connect() as connection:
        timeout = connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one()

    assert timeout == 5000


def test_file_database_uses_delete_journal_mode(tmp_path: Path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'avarch.adapters.sqlite.db'}")

    with engine.connect() as connection:
        journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()

    assert journal_mode == "delete"


def test_file_database_uses_full_synchronous_mode(tmp_path: Path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'avarch.adapters.sqlite.db'}")

    with engine.connect() as connection:
        synchronous = connection.exec_driver_sql("PRAGMA synchronous").scalar_one()

    assert synchronous == 2


def test_in_memory_database_does_not_require_wal() -> None:
    engine = create_db_engine("sqlite:///:memory:")

    with engine.connect() as connection:
        journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar_one()

    assert journal_mode == "memory"


def test_engine_allows_worker_thread_connections(tmp_path: Path) -> None:
    import threading

    engine = create_db_engine(f"sqlite:///{tmp_path / 'avarch.adapters.sqlite.db'}")
    create_db_schema(engine)
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            with Session(engine) as session:
                session.add(AppMeta(key="thread", value="ok"))
                session.commit()
        except BaseException as exc:  # pragma: no cover - surfaced in assertion below
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert errors == []


def test_insert_media_file(tmp_path: Path) -> None:
    db_path = tmp_path / "avarch.adapters.sqlite.db"
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
    db_path = tmp_path / "avarch.adapters.sqlite.db"
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
    db_path = tmp_path / "avarch.adapters.sqlite.db"
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
    db_path = tmp_path / "avarch.adapters.sqlite.db"
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
    db_path = tmp_path / "avarch.adapters.sqlite.db"
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
                source_fs_fingerprint=media_file.fs_fingerprint,
                created_at=now,
            )
        )
        session.commit()

        stored = session.exec(select(ProbeResult)).one()

    assert stored.media_file_id == media_file_id


def test_probe_result_requires_existing_media_file(tmp_path: Path) -> None:
    db_path = tmp_path / "avarch.adapters.sqlite.db"
    engine = create_db_engine(f"sqlite:///{db_path}")
    create_db_schema(engine)

    with Session(engine) as session:
        session.add(
            ProbeResult(
                media_file_id=999,
                ffprobe_json="{}",
                normalized_json="{}",
                probe_hash="hash",
                source_fs_fingerprint="fingerprint",
                created_at=datetime.now(UTC),
            )
        )

        with pytest.raises(IntegrityError):
            session.commit()


def test_multiple_probe_results_can_exist_for_one_file(tmp_path: Path) -> None:
    db_path = tmp_path / "avarch.adapters.sqlite.db"
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
                    source_fs_fingerprint=media_file.fs_fingerprint,
                    created_at=now,
                )
            )
        session.commit()

        rows = session.exec(select(ProbeResult)).all()

    assert len(rows) == 2


def test_probe_result_requires_source_fingerprint(tmp_path: Path) -> None:
    db_path = tmp_path / "avarch.adapters.sqlite.db"
    engine = create_db_engine(f"sqlite:///{db_path}")
    create_db_schema(engine)
    now = datetime.now(UTC)

    with engine.begin() as connection:
        result = connection.execute(
            sa.text(
                """
                INSERT INTO mediafile (
                    path,
                    size_bytes,
                    mtime_ns,
                    device_id,
                    inode,
                    fs_fingerprint,
                    discovered_at,
                    last_seen_at,
                    status
                )
                VALUES (
                    '/media/requires-fingerprint.mkv',
                    123,
                    456,
                    789,
                    101112,
                    'fingerprint',
                    :now,
                    :now,
                    'present'
                )
                """
            ),
            {"now": now},
        )
        with pytest.raises(IntegrityError):
            connection.execute(
                sa.text(
                    """
                    INSERT INTO proberesult (
                        media_file_id,
                        ffprobe_json,
                        normalized_json,
                        probe_hash,
                        source_fs_fingerprint,
                        created_at
                    )
                    VALUES (
                        :media_file_id,
                        '{}',
                        '{}',
                        'hash',
                        NULL,
                        :created_at
                    )
                    """
                ),
                {
                    "media_file_id": result.lastrowid,
                    "created_at": now,
                },
            )


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
