from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlmodel import Session, select

from avarch.adapters.filesystem.scanner import FileSnapshot, ScanResult
from avarch.adapters.sqlite.models import MediaFile, MediaFileStatus, ProbeResult


@dataclass(frozen=True, slots=True)
class FileSelection:
    selected: tuple[MediaFile, ...]
    missing: tuple[Path, ...]
    skipped_missing_status: tuple[MediaFile, ...]


class MediaFileNotFoundError(LookupError):
    pass


PathResolver = Callable[[Path], Path]


def require_media_file(session: Session, *, media_file_id: int) -> MediaFile:
    media_file = session.get(MediaFile, media_file_id)
    if media_file is None:
        raise MediaFileNotFoundError(f"Media file not found: {media_file_id}")
    return media_file


def update_inventory(
    session: Session,
    *,
    root: Path,
    snapshots: Sequence[FileSnapshot],
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
                    status=MediaFileStatus.ADDED,
                )
            )
            added += 1
            continue

        if _metadata_matches(media_file, snapshot):
            media_file.status = MediaFileStatus.PRESENT
            unchanged += 1
        else:
            media_file.size_bytes = snapshot.size_bytes
            media_file.mtime_ns = snapshot.mtime_ns
            media_file.device_id = snapshot.device_id
            media_file.inode = snapshot.inode
            media_file.fs_fingerprint = snapshot.fs_fingerprint
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


def select_inventory_files(
    session: Session,
    *,
    file_selectors: Sequence[Path] | None,
    workspace_root: Path,
    resolve_path: PathResolver,
    active_only: bool = True,
) -> FileSelection:
    if not file_selectors:
        statement = select(MediaFile).order_by(MediaFile.path)
        if active_only:
            statement = statement.where(MediaFile.status != MediaFileStatus.MISSING)
        return FileSelection(
            selected=tuple(session.exec(statement).all()),
            missing=(),
            skipped_missing_status=(),
        )

    media_files = list(session.exec(select(MediaFile).order_by(MediaFile.path)).all())
    by_key: dict[str, MediaFile] = {}
    for media_file in media_files:
        for key in _media_file_keys(media_file, workspace_root):
            by_key.setdefault(key, media_file)

    selected_by_id: dict[int, MediaFile] = {}
    missing: list[Path] = []
    skipped_missing_status: list[MediaFile] = []
    skipped_ids: set[int] = set()
    for selector in file_selectors:
        resolved = resolve_path(selector).resolve()
        media_file = by_key.get(_path_key(resolved))
        if media_file is None:
            missing.append(selector)
            continue
        media_file_id = media_file.id
        if media_file_id is None:
            continue
        if active_only and _status_value(media_file.status) == MediaFileStatus.MISSING.value:
            if media_file_id not in skipped_ids:
                skipped_missing_status.append(media_file)
                skipped_ids.add(media_file_id)
            continue
        selected_by_id.setdefault(media_file_id, media_file)

    selected = tuple(sorted(selected_by_id.values(), key=lambda item: item.path))
    return FileSelection(
        selected=selected,
        missing=tuple(missing),
        skipped_missing_status=tuple(sorted(skipped_missing_status, key=lambda item: item.path)),
    )


def active_inventory_files_with_missing_or_stale_probe(session: Session) -> tuple[MediaFile, ...]:
    media_files = list(
        session.exec(
            select(MediaFile)
            .where(MediaFile.status != MediaFileStatus.MISSING)
            .order_by(MediaFile.path)
        ).all()
    )
    return tuple(
        media_file for media_file in media_files if probe_is_missing_or_stale(session, media_file)
    )


def probe_is_missing_or_stale(session: Session, media_file: MediaFile) -> bool:
    if media_file.latest_probe_id is None:
        return True
    probe = session.get(ProbeResult, media_file.latest_probe_id)
    if probe is None:
        return True
    return probe.source_fs_fingerprint != media_file.fs_fingerprint


def _media_file_keys(media_file: MediaFile, workspace_root: Path) -> tuple[str, ...]:
    stored = Path(media_file.path)
    absolute = stored if stored.is_absolute() else workspace_root / stored
    keys = {_path_key(absolute.resolve())}
    if not stored.is_absolute():
        keys.add(_path_key(stored))
    return tuple(keys)


def _path_key(path: Path) -> str:
    return str(path)


def _status_value(status: MediaFileStatus | str) -> str:
    if isinstance(status, MediaFileStatus):
        return status.value
    return status
