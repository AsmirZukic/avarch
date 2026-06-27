from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class InventoryFileItem:
    path: str
    status: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class InventoryFileDetail:
    id: int
    path: str
    status: str
    size_bytes: int
    fs_fingerprint: str
    probe_hash: str | None
    normalized_probe_json: str | None
    current_plan_hash: str | None


class FileViewStore(Protocol):
    def list_files(self, *, changed_only: bool) -> list[InventoryFileItem]: ...

    def file_detail(
        self,
        *,
        file_selector: Path,
        workspace_root: Path,
        resolve_path: Callable[[Path], Path],
    ) -> InventoryFileDetail | None: ...


def list_inventory_files(
    store: FileViewStore,
    *,
    changed_only: bool,
) -> list[InventoryFileItem]:
    return store.list_files(changed_only=changed_only)


def inventory_file_detail(
    store: FileViewStore,
    *,
    file_selector: Path,
    workspace_root: Path,
    resolve_path: Callable[[Path], Path],
) -> InventoryFileDetail | None:
    return store.file_detail(
        file_selector=file_selector,
        workspace_root=workspace_root,
        resolve_path=resolve_path,
    )
