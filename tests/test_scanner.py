import os
from pathlib import Path

import pytest

from avarch.scanner import ScanError, build_content_key, create_file_snapshot, scan_root


def test_content_key_is_deterministic(tmp_path: Path) -> None:
    path = tmp_path / "movie.mkv"
    path.write_bytes(b"abc")
    snapshot = create_file_snapshot(path)

    first = build_content_key(
        path,
        size_bytes=snapshot.size_bytes,
        mtime_ns=snapshot.mtime_ns,
        device_id=snapshot.device_id,
        inode=snapshot.inode,
    )
    second = build_content_key(
        path,
        size_bytes=snapshot.size_bytes,
        mtime_ns=snapshot.mtime_ns,
        device_id=snapshot.device_id,
        inode=snapshot.inode,
    )

    assert first == second


def test_content_key_changes_when_size_changes(tmp_path: Path) -> None:
    path = tmp_path / "movie.mkv"
    path.write_bytes(b"abc")
    snapshot = create_file_snapshot(path)

    changed = build_content_key(
        path,
        size_bytes=snapshot.size_bytes + 1,
        mtime_ns=snapshot.mtime_ns,
        device_id=snapshot.device_id,
        inode=snapshot.inode,
    )

    assert changed != snapshot.content_key


def test_content_key_changes_when_mtime_changes(tmp_path: Path) -> None:
    path = tmp_path / "movie.mkv"
    path.write_bytes(b"abc")
    snapshot = create_file_snapshot(path)

    changed = build_content_key(
        path,
        size_bytes=snapshot.size_bytes,
        mtime_ns=snapshot.mtime_ns + 1,
        device_id=snapshot.device_id,
        inode=snapshot.inode,
    )

    assert changed != snapshot.content_key


def test_file_snapshot_contains_stat_metadata(tmp_path: Path) -> None:
    path = tmp_path / "movie.mkv"
    path.write_bytes(b"abc")
    os.utime(path, ns=(1_000_000_000, 2_000_000_000))

    snapshot = create_file_snapshot(path)

    assert snapshot.size_bytes == 3
    assert snapshot.mtime_ns == 2_000_000_000
    assert snapshot.device_id == path.stat().st_dev
    assert snapshot.inode == path.stat().st_ino


def test_file_snapshot_uses_absolute_path(tmp_path: Path) -> None:
    path = tmp_path / "movie.mkv"
    path.write_bytes(b"abc")

    snapshot = create_file_snapshot(path)

    assert snapshot.path == path.resolve()


def test_scan_root_finds_supported_media(tmp_path: Path) -> None:
    movie = tmp_path / "movie.mkv"
    movie.write_bytes(b"abc")

    snapshots = scan_root(tmp_path, extensions={".mkv"}, exclude_directories=set())

    assert [snapshot.path for snapshot in snapshots] == [movie.resolve()]


def test_scan_root_is_recursive(tmp_path: Path) -> None:
    nested = tmp_path / "nested"
    nested.mkdir()
    movie = nested / "movie.mkv"
    movie.write_bytes(b"abc")

    snapshots = scan_root(tmp_path, extensions={".mkv"}, exclude_directories=set())

    assert [snapshot.path for snapshot in snapshots] == [movie.resolve()]


def test_scan_root_ignores_unsupported_extensions(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_text("ignore", encoding="utf-8")

    snapshots = scan_root(tmp_path, extensions={".mkv"}, exclude_directories=set())

    assert snapshots == []


def test_scan_root_accepts_uppercase_extensions(tmp_path: Path) -> None:
    movie = tmp_path / "movie.MKV"
    movie.write_bytes(b"abc")

    snapshots = scan_root(tmp_path, extensions={".mkv"}, exclude_directories=set())

    assert [snapshot.path for snapshot in snapshots] == [movie.resolve()]


def test_scan_root_skips_excluded_directories(tmp_path: Path) -> None:
    work_dir = tmp_path / ".avarch-work"
    work_dir.mkdir()
    (work_dir / "temporary.mkv").write_bytes(b"abc")

    snapshots = scan_root(
        tmp_path,
        extensions={".mkv"},
        exclude_directories={".avarch-work"},
    )

    assert snapshots == []


def test_scan_root_does_not_follow_symlinks(tmp_path: Path) -> None:
    target_dir = tmp_path / "target"
    target_dir.mkdir()
    (target_dir / "movie.mkv").write_bytes(b"abc")
    (tmp_path / "linked-dir").symlink_to(target_dir, target_is_directory=True)
    (tmp_path / "linked-file.mkv").symlink_to(target_dir / "movie.mkv")

    snapshots = scan_root(tmp_path, extensions={".mkv"}, exclude_directories=set())

    assert [snapshot.path for snapshot in snapshots] == [(target_dir / "movie.mkv").resolve()]


def test_scan_root_returns_sorted_results(tmp_path: Path) -> None:
    second = tmp_path / "b.mkv"
    first = tmp_path / "a.mkv"
    second.write_bytes(b"b")
    first.write_bytes(b"a")

    snapshots = scan_root(tmp_path, extensions={".mkv"}, exclude_directories=set())

    assert [snapshot.path for snapshot in snapshots] == [first.resolve(), second.resolve()]


def test_scan_root_rejects_missing_root(tmp_path: Path) -> None:
    with pytest.raises(ScanError):
        scan_root(tmp_path / "missing", extensions={".mkv"}, exclude_directories=set())


def test_scan_root_rejects_file_as_root(tmp_path: Path) -> None:
    path = tmp_path / "movie.mkv"
    path.write_bytes(b"abc")

    with pytest.raises(ScanError):
        scan_root(path, extensions={".mkv"}, exclude_directories=set())
