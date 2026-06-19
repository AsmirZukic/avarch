from __future__ import annotations

import hashlib
import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlmodel import Session, select

from avarch.models.db import MediaFile, MediaFileStatus


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


def update_inventory(
    session: Session,
    *,
    root: Path,
    snapshots: Sequence[FileSnapshot],
    scanned_at: datetime,
    workspace_root: Path | None = None,
) -> ScanResult:
    root = root.resolve()
    workspace_root = workspace_root.resolve() if workspace_root is not None else None
    snapshot_by_path = {snapshot.path: snapshot for snapshot in snapshots}
    known_files = session.exec(select(MediaFile)).all()
    known_by_path = {
        _absolute_media_path(media_file.path, workspace_root): media_file
        for media_file in known_files
        if _is_relative_to(_absolute_media_path(media_file.path, workspace_root), root)
    }

    added = 0
    changed = 0
    unchanged = 0

    for path, snapshot in snapshot_by_path.items():
        media_file = known_by_path.get(path)
        if media_file is None:
            session.add(
                MediaFile(
                    path=_stored_media_path(path, workspace_root),
                    size_bytes=snapshot.size_bytes,
                    mtime_ns=snapshot.mtime_ns,
                    device_id=snapshot.device_id,
                    inode=snapshot.inode,
                    fs_fingerprint=snapshot.fs_fingerprint,
                    discovered_at=scanned_at,
                    last_seen_at=scanned_at,
                    status=MediaFileStatus.ADDED,
                )
            )
            added += 1
            continue

        if _metadata_matches(media_file, snapshot):
            media_file.last_seen_at = scanned_at
            media_file.status = MediaFileStatus.PRESENT
            unchanged += 1
        else:
            media_file.size_bytes = snapshot.size_bytes
            media_file.mtime_ns = snapshot.mtime_ns
            media_file.device_id = snapshot.device_id
            media_file.inode = snapshot.inode
            media_file.fs_fingerprint = snapshot.fs_fingerprint
            media_file.last_seen_at = scanned_at
            media_file.status = MediaFileStatus.CHANGED
            changed += 1

        session.add(media_file)

    missing = 0
    for path, media_file in known_by_path.items():
        if path not in snapshot_by_path:
            media_file.status = MediaFileStatus.MISSING
            session.add(media_file)
            missing += 1

    return ScanResult(
        root=root,
        added=added,
        changed=changed,
        missing=missing,
        unchanged=unchanged,
    )


def _metadata_matches(media_file: MediaFile, snapshot: FileSnapshot) -> bool:
    return (
        media_file.size_bytes == snapshot.size_bytes
        and media_file.mtime_ns == snapshot.mtime_ns
        and media_file.device_id == snapshot.device_id
        and media_file.inode == snapshot.inode
        and media_file.fs_fingerprint == snapshot.fs_fingerprint
    )


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
    except ValueError:
        return False
    return True


def _absolute_media_path(value: str, workspace_root: Path | None) -> Path:
    path = Path(value)
    if path.is_absolute() or workspace_root is None:
        return path
    return workspace_root / path


def _stored_media_path(path: Path, workspace_root: Path | None) -> str:
    if workspace_root is None:
        return str(path)
    try:
        return str(path.resolve().relative_to(workspace_root))
    except ValueError:
        return str(path)
