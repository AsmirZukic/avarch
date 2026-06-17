from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class LibraryProbeState(StrEnum):
    CURRENT = "current"
    MISSING = "missing"
    STALE = "stale"
    UNREADABLE = "unreadable"


@dataclass(frozen=True, slots=True)
class LibraryRow:
    media_file_id: int
    path: str
    inventory_status: str
    probe_state: LibraryProbeState
    container: str | None
    video_codec: str | None
    resolution: str | None
    duration_seconds: float | None
    last_scanned_at: str
    selectable: bool = True


@dataclass(frozen=True, slots=True)
class LibraryFilters:
    root: Path | None = None
    inventory_statuses: frozenset[str] | None = None
    probe_states: frozenset[LibraryProbeState] | None = None
    video_codec: str | None = None
    search_text: str | None = None


@dataclass(frozen=True, slots=True)
class LibrarySnapshot:
    rows: tuple[LibraryRow, ...]
