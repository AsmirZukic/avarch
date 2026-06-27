from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from avarch.config import AppConfig


class InventoryScanError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class InventoryScanResult:
    root: Path
    added: int
    changed: int
    missing: int
    unchanged: int


class InventoryScanWorkflow(Protocol):
    def scan_root(
        self,
        *,
        root: Path,
        config: AppConfig,
        workspace_root: Path | None,
        scanned_at: datetime,
    ) -> InventoryScanResult: ...


def scan_inventory_root(
    workflow: InventoryScanWorkflow,
    *,
    root: Path,
    config: AppConfig,
    workspace_root: Path | None,
    scanned_at: datetime,
) -> InventoryScanResult:
    return workflow.scan_root(
        root=root,
        config=config,
        workspace_root=workspace_root,
        scanned_at=scanned_at,
    )
