from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from avarch.models.probe import NormalizedProbe


class ProbeWorkflowError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProbeInputFile:
    id: int
    path: str
    fs_fingerprint: str
    probe_current: bool


@dataclass(frozen=True, slots=True)
class ProbeInputSelection:
    selected: tuple[ProbeInputFile, ...]
    missing: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class StoredProbeResult:
    probe_hash: str
    normalized_probe: NormalizedProbe


@dataclass(frozen=True, slots=True)
class ProbeFileResult:
    path: Path
    probe_hash: str
    normalized_probe: NormalizedProbe


@dataclass(frozen=True, slots=True)
class ProbeCommandResult:
    selected: int
    processed: int
    skipped: int
    failed: int
    results: tuple[ProbeFileResult, ...]
    messages: tuple[str, ...]


class ProbeStore(Protocol):
    def select_inputs(
        self,
        *,
        file_selectors: Sequence[Path] | None,
        workspace_root: Path,
        resolve_path: Callable[[Path], Path],
    ) -> ProbeInputSelection: ...

    def stale_or_missing_probe_inputs(self) -> tuple[ProbeInputFile, ...]: ...

    def store_probe_result(
        self,
        *,
        media_file_id: int,
        normalized_probe: NormalizedProbe,
        created_at: datetime,
    ) -> StoredProbeResult: ...


class ProbeCollector(Protocol):
    def collect(self, path: Path) -> tuple[Mapping[str, Any], NormalizedProbe]: ...


def run_probe_workflow(
    store: ProbeStore,
    *,
    collector: ProbeCollector,
    file_selectors: Sequence[Path] | None,
    workspace_root: Path,
    resolve_path: Callable[[Path], Path],
    force: bool,
    now: datetime,
) -> ProbeCommandResult:
    if file_selectors:
        selection = store.select_inputs(
            file_selectors=file_selectors,
            workspace_root=workspace_root,
            resolve_path=resolve_path,
        )
        if selection.missing:
            raise ProbeWorkflowError(
                f"File is not present in the media inventory: {selection.missing[0]}"
            )
        media_files = list(selection.selected)
    else:
        media_files = list(store.stale_or_missing_probe_inputs())

    processed = 0
    skipped = 0
    failed = 0
    results: list[ProbeFileResult] = []
    messages: list[str] = []

    for media_file in media_files:
        display_path = _display_media_path(media_file.path, workspace_root)
        if not force and media_file.probe_current:
            skipped += 1
            messages.append(f"Skipped {display_path}: probe-current")
            continue

        file_path = _absolute_media_path(media_file.path, workspace_root)
        if not file_path.exists():
            failed += 1
            messages.append(f"Failed {display_path}: file-missing")
            continue
        if not file_path.is_file():
            failed += 1
            messages.append(f"Failed {display_path}: not-regular-file")
            continue

        try:
            _, normalized_probe = collector.collect(file_path)
            stored = store.store_probe_result(
                media_file_id=media_file.id,
                normalized_probe=normalized_probe,
                created_at=now,
            )
        except Exception as exc:
            failed += 1
            messages.append(f"Failed {display_path}: {exc}")
            continue

        processed += 1
        results.append(
            ProbeFileResult(
                path=file_path,
                probe_hash=stored.probe_hash,
                normalized_probe=stored.normalized_probe,
            )
        )

    return ProbeCommandResult(
        selected=len(media_files),
        processed=processed,
        skipped=skipped,
        failed=failed,
        results=tuple(results),
        messages=tuple(messages),
    )


def _absolute_media_path(path_value: str, workspace_root: Path) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path
    return workspace_root / path


def _display_media_path(path_value: str, workspace_root: Path) -> str:
    return str(_absolute_media_path(path_value, workspace_root))
