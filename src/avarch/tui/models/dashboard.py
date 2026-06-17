from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from avarch.tui.models.common import UiRevision


@dataclass(frozen=True, slots=True)
class QueueTotals:
    pending: int = 0
    running: int = 0
    held: int = 0
    failed: int = 0
    validated: int = 0
    canceled: int = 0
    completed: int = 0
    skipped: int = 0


@dataclass(frozen=True, slots=True)
class SchedulerSummary:
    mode: str
    lease_state: str
    runner_id: str | None
    heartbeat_at: datetime | None
    lease_expires_at: datetime | None
    control_generation: int
    acknowledged_generation: int
    cancel_pending: int
    hold_pending: int


@dataclass(frozen=True, slots=True)
class DashboardJobSummary:
    job_id: int
    file_name: str
    source_path: str
    status: str
    stage: str
    profile_name: str
    priority: int
    updated_at: datetime
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class DashboardSnapshot:
    revision: UiRevision
    scheduler: SchedulerSummary
    queue_totals: QueueTotals
    active_jobs: tuple[DashboardJobSummary, ...]
    recent_failures: tuple[DashboardJobSummary, ...]
    promotion_ready: tuple[DashboardJobSummary, ...]
