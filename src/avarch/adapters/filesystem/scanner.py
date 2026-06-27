from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path


class ScanError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    path: Path
    size_bytes: int
    mtime_ns: int
    device_id: int
    inode: int
    fs_fingerprint: str


@dataclass(frozen=True, slots=True)
class ScanResult:
    root: Path
    added: int
    changed: int
    missing: int
    unchanged: int


def build_fs_fingerprint(
    path: Path,
    *,
    size_bytes: int,
    mtime_ns: int,
    device_id: int,
    inode: int,
) -> str:
    """Cheap filesystem fingerprint derived from path and stat metadata.

    This is used for change detection and probe freshness. It is not a hash
    of the file contents and must not be treated as a content-integrity proof.
    """
    payload = "\n".join(
        (
            "fs-v1",
            str(path.resolve()),
            str(size_bytes),
            str(mtime_ns),
            str(device_id),
            str(inode),
        )
    )
    return hashlib.blake2b(payload.encode("utf-8"), digest_size=16).hexdigest()


def create_file_snapshot(path: Path) -> FileSnapshot:
    absolute_path = path.resolve()
    stat_result = path.stat(follow_symlinks=False)
    return FileSnapshot(
        path=absolute_path,
        size_bytes=stat_result.st_size,
        mtime_ns=stat_result.st_mtime_ns,
        device_id=stat_result.st_dev,
        inode=stat_result.st_ino,
        fs_fingerprint=build_fs_fingerprint(
            absolute_path,
            size_bytes=stat_result.st_size,
            mtime_ns=stat_result.st_mtime_ns,
            device_id=stat_result.st_dev,
            inode=stat_result.st_ino,
        ),
    )


def scan_root(
    root: Path,
    *,
    extensions: set[str],
    exclude_directories: set[str],
) -> list[FileSnapshot]:
    root = root.resolve()
    if not root.exists():
        raise ScanError(f"Root does not exist: {root}")
    if not root.is_dir():
        raise ScanError(f"Root is not a directory: {root}")

    normalized_extensions = {extension.lower() for extension in extensions}
    snapshots: list[FileSnapshot] = []

    try:
        for directory, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = [
                dirname
                for dirname in dirnames
                if dirname not in exclude_directories
                and not (Path(directory) / dirname).is_symlink()
            ]

            for filename in filenames:
                if filename.endswith(".avarch-original"):
                    continue
                path = Path(directory) / filename
                if path.is_symlink() or path.suffix.lower() not in normalized_extensions:
                    continue
                snapshots.append(create_file_snapshot(path))
    except OSError as exc:
        raise ScanError(f"Unable to scan root: {root}") from exc

    return sorted(snapshots, key=lambda snapshot: str(snapshot.path))
