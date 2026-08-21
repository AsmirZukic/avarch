from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from sqlmodel import Session

from avarch.adapters.sqlite.inventory import (
    active_inventory_files_with_missing_or_stale_probe,
    probe_is_missing_or_stale,
    select_inventory_files,
)
from avarch.adapters.sqlite.models import MediaFile
from avarch.adapters.sqlite.probes import store_probe_result
from avarch.application.probing import (
    ProbeInputFile,
    ProbeInputSelection,
    StoredProbeResult,
)
from avarch.models.probe import NormalizedProbe


class SqliteProbeStore:
    def __init__(self, session: Session) -> None:
        self._session = session

    def select_inputs(
        self,
        *,
        file_selectors: Sequence[Path] | None,
        workspace_root: Path,
        resolve_path: Callable[[Path], Path],
    ) -> ProbeInputSelection:
        selection = select_inventory_files(
            self._session,
            file_selectors=file_selectors,
            workspace_root=workspace_root,
            resolve_path=resolve_path,
        )
        return ProbeInputSelection(
            selected=tuple(
                _probe_input(self._session, media_file) for media_file in selection.selected
            ),
            missing=selection.missing,
        )

    def stale_or_missing_probe_inputs(self) -> tuple[ProbeInputFile, ...]:
        return tuple(
            _probe_input(self._session, media_file)
            for media_file in active_inventory_files_with_missing_or_stale_probe(self._session)
        )

    def store_probe_result(
        self,
        *,
        media_file_id: int,
        normalized_probe: NormalizedProbe,
        created_at: datetime,
    ) -> StoredProbeResult:
        media_file = self._session.get(MediaFile, media_file_id)
        if media_file is None:
            raise RuntimeError(f"Media file not found: {media_file_id}")
        try:
            result = store_probe_result(
                self._session,
                media_file=media_file,
                normalized_probe=normalized_probe,
                created_at=created_at,
            )
            self._session.commit()
            self._session.refresh(result)
        except Exception:
            self._session.rollback()
            raise
        return StoredProbeResult(
            probe_hash=result.probe_hash,
            normalized_probe=normalized_probe,
        )


def _probe_input(session: Session, media_file: MediaFile) -> ProbeInputFile:
    if media_file.id is None:
        raise RuntimeError("Expected a persisted media file.")
    return ProbeInputFile(
        id=media_file.id,
        path=media_file.path,
        fs_fingerprint=media_file.fs_fingerprint,
        probe_current=not probe_is_missing_or_stale(session, media_file),
    )
