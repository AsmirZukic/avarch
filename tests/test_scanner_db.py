import os
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from avarch.adapters.filesystem.scanner import (
    ScanError,
    ScanResult,
    create_file_snapshot,
    scan_root,
)
from avarch.adapters.sqlite.db import create_db_engine, create_db_schema
from avarch.adapters.sqlite.inventory import update_inventory
from avarch.adapters.sqlite.models import MediaFile, MediaFileStatus


def test_initial_scan_inserts_media_files(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"abc")

    with Session(engine) as session:
        with session.begin():
            update_inventory(
                session,
                root=tmp_path,
                snapshots=[create_file_snapshot(media)],
            )
        rows = session.exec(select(MediaFile)).all()

    assert len(rows) == 1


def test_initial_scan_marks_files_added(tmp_path: Path) -> None:
    media_file = _scan_one_file(tmp_path)

    assert media_file.status == MediaFileStatus.ADDED


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
                status=MediaFileStatus.ADDED,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_second_identical_scan_does_not_add_rows(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"abc")
    _scan(engine, tmp_path, [media])

    result = _scan(engine, tmp_path, [media])

    with Session(engine) as session:
        rows = session.exec(select(MediaFile)).all()
    assert len(rows) == 1
    assert result.added == 0


def test_second_identical_scan_marks_file_present(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"abc")
    _scan(engine, tmp_path, [media])

    _scan(engine, tmp_path, [media])

    assert _one_media_file(engine).status == MediaFileStatus.PRESENT


def test_changed_size_marks_file_changed(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"abc")
    _scan(engine, tmp_path, [media])
    media.write_bytes(b"abcd")

    _scan(engine, tmp_path, [media])

    assert _one_media_file(engine).status == MediaFileStatus.CHANGED


def test_changed_mtime_marks_file_changed(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"abc")
    _scan(engine, tmp_path, [media])
    os.utime(media, ns=(1_900_000_000_000_000_000, 1_900_000_000_000_000_000))

    _scan(engine, tmp_path, [media])

    assert _one_media_file(engine).status == MediaFileStatus.CHANGED


def test_replaced_file_at_same_path_is_changed(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"abc")
    _scan(engine, tmp_path, [media])
    media.unlink()
    media.write_bytes(b"replacement")

    _scan(engine, tmp_path, [media])

    assert _one_media_file(engine).status == MediaFileStatus.CHANGED


def test_removed_file_is_marked_missing(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"abc")
    _scan(engine, tmp_path, [media])
    media.unlink()

    result = _scan(engine, tmp_path, [])

    assert result.missing == 1
    assert _one_media_file(engine).status == MediaFileStatus.MISSING


def test_missing_file_row_is_not_deleted(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"abc")
    _scan(engine, tmp_path, [media])
    media.unlink()

    _scan(engine, tmp_path, [])

    with Session(engine) as session:
        rows = session.exec(select(MediaFile)).all()
    assert len(rows) == 1


def test_reappearing_file_is_marked_changed(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"abc")
    _scan(engine, tmp_path, [media])
    media.unlink()
    _scan(engine, tmp_path, [])
    media.write_bytes(b"abc")
    os.utime(media, ns=(1_900_000_000_000_000_000, 1_900_000_000_000_000_000))

    _scan(engine, tmp_path, [media])

    assert _one_media_file(engine).status == MediaFileStatus.CHANGED


def test_failed_traversal_does_not_mark_files_missing(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"abc")
    _scan(engine, tmp_path, [media])
    media.unlink()

    with pytest.raises(ScanError):
        scan_root(tmp_path / "missing-root", extensions={".mkv"}, exclude_directories=set())

    assert _one_media_file(engine).status == MediaFileStatus.ADDED


def _engine(tmp_path: Path) -> Engine:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'avarch.db'}")
    create_db_schema(engine)
    return engine


def _scan_one_file(tmp_path: Path) -> MediaFile:
    engine = _engine(tmp_path)
    media = tmp_path / "movie.mkv"
    media.write_bytes(b"abc")
    _scan(engine, tmp_path, [media])
    return _one_media_file(engine)


def _scan(
    engine: Engine,
    root: Path,
    paths: list[Path],
) -> ScanResult:
    snapshots = [create_file_snapshot(path) for path in paths]
    with Session(engine) as session, session.begin():
        return update_inventory(
            session,
            root=root,
            snapshots=snapshots,
        )


def _one_media_file(engine: Engine) -> MediaFile:
    with Session(engine) as session:
        return session.exec(select(MediaFile)).one()
