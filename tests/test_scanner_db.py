import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from avarch.adapters.sqlite.db import create_db_engine, create_db_schema
from avarch.adapters.sqlite.inventory import update_inventory
from avarch.adapters.sqlite.models import MediaFile, MediaFileStatus
from avarch.scanner import ScanError, ScanResult, create_file_snapshot, scan_root


def test_initial_scan_inserts_media_files(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"abc")
    scanned_at = _now()

    with Session(engine) as session:
        with session.begin():
            update_inventory(
                session,
                root=tmp_path,
                snapshots=[create_file_snapshot(media)],
                scanned_at=scanned_at,
            )
        rows = session.exec(select(MediaFile)).all()

    assert len(rows) == 1


def test_initial_scan_marks_files_added(tmp_path: Path) -> None:
    media_file = _scan_one_file(tmp_path)

    assert media_file.status == MediaFileStatus.ADDED


def test_initial_scan_sets_discovered_and_last_seen_times(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"abc")
    scanned_at = _now()

    with Session(engine) as session:
        with session.begin():
            update_inventory(
                session,
                root=tmp_path,
                snapshots=[create_file_snapshot(media)],
                scanned_at=scanned_at,
            )
        stored = session.exec(select(MediaFile)).one()

    assert stored.discovered_at == scanned_at.replace(tzinfo=None)
    assert stored.last_seen_at == scanned_at.replace(tzinfo=None)


def test_duplicate_paths_are_not_inserted(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"abc")
    snapshot = create_file_snapshot(media)

    with Session(engine) as session:
        session.add(
            MediaFile(
                path=str(snapshot.path),
                size_bytes=snapshot.size_bytes,
                mtime_ns=snapshot.mtime_ns,
                device_id=snapshot.device_id,
                inode=snapshot.inode,
                fs_fingerprint=snapshot.fs_fingerprint,
                discovered_at=_now(),
                last_seen_at=_now(),
                status=MediaFileStatus.ADDED,
            )
        )
        session.add(
            MediaFile(
                path=str(snapshot.path),
                size_bytes=snapshot.size_bytes,
                mtime_ns=snapshot.mtime_ns,
                device_id=snapshot.device_id,
                inode=snapshot.inode,
                fs_fingerprint="other",
                discovered_at=_now(),
                last_seen_at=_now(),
                status=MediaFileStatus.ADDED,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_second_identical_scan_does_not_add_rows(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"abc")
    _scan(engine, tmp_path, [media], _now())

    result = _scan(engine, tmp_path, [media], _now() + timedelta(seconds=1))

    with Session(engine) as session:
        rows = session.exec(select(MediaFile)).all()
    assert len(rows) == 1
    assert result.added == 0


def test_second_identical_scan_marks_file_present(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"abc")
    _scan(engine, tmp_path, [media], _now())

    _scan(engine, tmp_path, [media], _now() + timedelta(seconds=1))

    assert _one_media_file(engine).status == MediaFileStatus.PRESENT


def test_unchanged_scan_preserves_discovered_at(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"abc")
    first_scan = _now()
    _scan(engine, tmp_path, [media], first_scan)

    _scan(engine, tmp_path, [media], first_scan + timedelta(seconds=10))

    assert _one_media_file(engine).discovered_at == first_scan.replace(tzinfo=None)


def test_changed_size_marks_file_changed(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"abc")
    _scan(engine, tmp_path, [media], _now())
    media.write_bytes(b"abcd")

    _scan(engine, tmp_path, [media], _now() + timedelta(seconds=1))

    assert _one_media_file(engine).status == MediaFileStatus.CHANGED


def test_changed_mtime_marks_file_changed(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"abc")
    _scan(engine, tmp_path, [media], _now())
    os.utime(media, ns=(1_900_000_000_000_000_000, 1_900_000_000_000_000_000))

    _scan(engine, tmp_path, [media], _now() + timedelta(seconds=1))

    assert _one_media_file(engine).status == MediaFileStatus.CHANGED


def test_replaced_file_at_same_path_is_changed(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"abc")
    _scan(engine, tmp_path, [media], _now())
    media.unlink()
    media.write_bytes(b"replacement")

    _scan(engine, tmp_path, [media], _now() + timedelta(seconds=1))

    assert _one_media_file(engine).status == MediaFileStatus.CHANGED


def test_removed_file_is_marked_missing(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"abc")
    _scan(engine, tmp_path, [media], _now())
    media.unlink()

    result = _scan(engine, tmp_path, [], _now() + timedelta(seconds=1))

    assert result.missing == 1
    assert _one_media_file(engine).status == MediaFileStatus.MISSING


def test_missing_file_row_is_not_deleted(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"abc")
    _scan(engine, tmp_path, [media], _now())
    media.unlink()

    _scan(engine, tmp_path, [], _now() + timedelta(seconds=1))

    with Session(engine) as session:
        rows = session.exec(select(MediaFile)).all()
    assert len(rows) == 1


def test_missing_file_keeps_last_seen_at(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"abc")
    first_scan = _now()
    _scan(engine, tmp_path, [media], first_scan)
    media.unlink()

    _scan(engine, tmp_path, [], first_scan + timedelta(seconds=10))

    assert _one_media_file(engine).last_seen_at == first_scan.replace(tzinfo=None)


def test_reappearing_file_is_marked_changed(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"abc")
    _scan(engine, tmp_path, [media], _now())
    media.unlink()
    _scan(engine, tmp_path, [], _now() + timedelta(seconds=1))
    media.write_bytes(b"abc")
    os.utime(media, ns=(1_900_000_000_000_000_000, 1_900_000_000_000_000_000))

    _scan(engine, tmp_path, [media], _now() + timedelta(seconds=2))

    assert _one_media_file(engine).status == MediaFileStatus.CHANGED


def test_failed_traversal_does_not_mark_files_missing(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"abc")
    _scan(engine, tmp_path, [media], _now())
    media.unlink()

    with pytest.raises(ScanError):
        scan_root(tmp_path / "missing-root", extensions={".mkv"}, exclude_directories=set())

    assert _one_media_file(engine).status == MediaFileStatus.ADDED


def _engine(tmp_path: Path) -> Engine:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'avarch.adapters.sqlite.db'}")
    create_db_schema(engine)
    return engine


def _scan_one_file(tmp_path: Path) -> MediaFile:
    engine = _engine(tmp_path)
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"abc")
    _scan(engine, tmp_path, [media], _now())
    return _one_media_file(engine)


def _scan(
    engine: Engine,
    root: Path,
    paths: list[Path],
    scanned_at: datetime,
) -> ScanResult:
    snapshots = [create_file_snapshot(path) for path in paths]
    with Session(engine) as session, session.begin():
        return update_inventory(
            session,
            root=root,
            snapshots=snapshots,
            scanned_at=scanned_at,
        )


def _one_media_file(engine: Engine) -> MediaFile:
    with Session(engine) as session:
        return session.exec(select(MediaFile)).one()


def _now() -> datetime:
    return datetime(2026, 6, 14, tzinfo=UTC)
