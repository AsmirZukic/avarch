from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class CandidateState(StrEnum):
    READY = "ready"
    NEEDS_ANALYSIS = "needs_analysis"
    ALREADY_SATISFIED = "already_satisfied"
    BLOCKED = "blocked"
    EXCLUDED = "excluded"


@dataclass(frozen=True, slots=True)
class DirectoryEntry:
    path: Path
    name: str
    is_dir: bool
    is_hidden: bool


@dataclass(frozen=True, slots=True)
class DirectoryListing:
    path: Path
    parent: Path | None
    entries: tuple[DirectoryEntry, ...]


@dataclass(frozen=True, slots=True)
class ScanSummary:
    roots: tuple[Path, ...]
    added: int
    changed: int
    missing: int
    unchanged: int


@dataclass(frozen=True, slots=True)
class AnalysisSummary:
    requested: int
    completed: int
    failed: int


@dataclass(frozen=True, slots=True)
class CandidateRow:
    media_file_id: int
    path: str
    state: CandidateState
    reason: str
    eligible: bool


@dataclass(frozen=True, slots=True)
class CandidateSnapshot:
    rows: tuple[CandidateRow, ...]


@dataclass(frozen=True, slots=True)
class WorkflowPreview:
    media_file_ids: tuple[int, ...]
    profile_name: str
    profile_effective_hash: str
    summary: str


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    created: int
    already_queued: int
    already_completed: int
    not_eligible: int
