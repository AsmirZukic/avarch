from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from sqlmodel import Session, select

from avarch.models.db import MediaFile, MediaFileStatus, ProbeResult


class InventorySelectionError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class FileSelection:
    selected: tuple[MediaFile, ...]
    missing: tuple[Path, ...]
    skipped_missing_status: tuple[MediaFile, ...]


PathResolver = Callable[[Path], Path]


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
        media_file
        for media_file in media_files
        if probe_is_missing_or_stale(session, media_file)
    )


def probe_is_missing_or_stale(session: Session, media_file: MediaFile) -> bool:
    if media_file.latest_probe_id is None:
        return True
    probe = session.get(ProbeResult, media_file.latest_probe_id)
    if probe is None:
        return True
    return probe.source_fs_fingerprint != media_file.fs_fingerprint


def resolve_single_inventory_file(
    session: Session,
    *,
    path: Path,
    workspace_root: Path,
    resolve_path: PathResolver,
) -> MediaFile | None:
    selection = select_inventory_files(
        session,
        file_selectors=[path],
        workspace_root=workspace_root,
        resolve_path=resolve_path,
        active_only=False,
    )
    return selection.selected[0] if selection.selected else None


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
