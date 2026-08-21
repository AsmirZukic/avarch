from __future__ import annotations

from pathlib import Path

from sqlmodel import Session

from avarch.adapters.filesystem.scanner import ScanError, scan_root
from avarch.adapters.sqlite.db import create_db_engine
from avarch.adapters.sqlite.inventory import update_inventory
from avarch.application.inventory_scan import InventoryScanError, InventoryScanResult
from avarch.config import AppConfig


class SqliteInventoryScanWorkflow:
    def __init__(self, *, database_url: str) -> None:
        self._engine = create_db_engine(database_url)

    def scan_root(
        self,
        *,
        root: Path,
        config: AppConfig,
        workspace_root: Path | None,
    ) -> InventoryScanResult:
        try:
            snapshots = scan_root(
                root,
                extensions=config.scanner.extensions,
                exclude_directories=config.scanner.exclude_directories,
            )
            with Session(self._engine) as session, session.begin():
                result = update_inventory(
                    session,
                    root=root,
                    snapshots=snapshots,
                    workspace_root=workspace_root,
                )
        except ScanError as exc:
            raise InventoryScanError(str(exc)) from exc
        return InventoryScanResult(
            root=result.root,
            added=result.added,
            changed=result.changed,
            missing=result.missing,
            unchanged=result.unchanged,
        )
