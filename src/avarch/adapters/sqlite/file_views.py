from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from sqlmodel import Session, select

from avarch.adapters.sqlite.inventory import select_inventory_files
from avarch.adapters.sqlite.models import MediaFile, MediaFileStatus
from avarch.adapters.sqlite.planning import current_plan_for_file
from avarch.adapters.sqlite.probes import get_canonical_probe_result
from avarch.application.file_views import InventoryFileDetail, InventoryFileItem


class SqliteFileViewStore:
    def __init__(self, session: Session) -> None:
        self._session = session

    def list_files(self, *, changed_only: bool) -> list[InventoryFileItem]:
        rows = list(self._session.exec(select(MediaFile).order_by(MediaFile.path)).all())
        if changed_only:
            changed_statuses = {
                MediaFileStatus.ADDED.value,
                MediaFileStatus.CHANGED.value,
                MediaFileStatus.MISSING.value,
            }
            rows = [row for row in rows if _status_value(row.status) in changed_statuses]
        return [
            InventoryFileItem(
                path=row.path,
                status=_status_value(row.status),
                size_bytes=row.size_bytes,
            )
            for row in rows
        ]

    def file_detail(
        self,
        *,
        file_selector: Path,
        workspace_root: Path,
        resolve_path: Callable[[Path], Path],
    ) -> InventoryFileDetail | None:
        selection = select_inventory_files(
            self._session,
            file_selectors=[file_selector],
            workspace_root=workspace_root,
            resolve_path=resolve_path,
            active_only=False,
        )
        if selection.missing or not selection.selected:
            return None
        media_file = selection.selected[0]
        if media_file.id is None:
            return None
        probe_result = get_canonical_probe_result(self._session, media_file)
        current_plan = current_plan_for_file(self._session, media_file)
        return InventoryFileDetail(
            id=media_file.id,
            path=media_file.path,
            status=_status_value(media_file.status),
            size_bytes=media_file.size_bytes,
            fs_fingerprint=media_file.fs_fingerprint,
            probe_hash=probe_result.probe_hash if probe_result is not None else None,
            normalized_probe_json=(
                probe_result.normalized_json if probe_result is not None else None
            ),
            current_plan_hash=current_plan.plan_hash if current_plan is not None else None,
        )


def _status_value(status: MediaFileStatus | str) -> str:
    if isinstance(status, MediaFileStatus):
        return status.value
    return status
