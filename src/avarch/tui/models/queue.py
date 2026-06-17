from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from avarch.models.scheduler import JobStage, JobStatus
from avarch.tui.models.common import UiRevision
from avarch.tui.models.dashboard import QueueTotals, SchedulerSummary


@dataclass(frozen=True, slots=True)
class QueueFilters:
    statuses: frozenset[JobStatus] | None = None
    stages: frozenset[JobStage] | None = None
    profile_name: str | None = None
    search_text: str | None = None
    active_only: bool = False
    failures_only: bool = False
    promotion_ready_only: bool = False
    limit: int | None = 100


@dataclass(frozen=True, slots=True)
class QueueJobRow:
    job_id: int
    status: str
    stage: str
    priority: int
    attempts: int
    progress: str
    profile_name: str
    file_name: str
    source_path: str
    updated_at: datetime
    control: str | None = None


@dataclass(frozen=True, slots=True)
class QueueSnapshot:
    revision: UiRevision
    scheduler: SchedulerSummary
    totals: QueueTotals
    filters: QueueFilters
    rows: tuple[QueueJobRow, ...]


@dataclass(frozen=True, slots=True)
class QueueClearFilters:
    statuses: frozenset[JobStatus] | None = None
    stages: frozenset[JobStage] | None = None
    profile_name: str | None = None
    selected_job_ids: frozenset[int] = frozenset()
    include_running_scheduler_jobs: bool = False


@dataclass(frozen=True, slots=True)
class QueueClearPreview:
    filters: QueueClearFilters
    matched: int
    cancel_immediately: int
    request_interruption: int
    active_promotions_excluded: int
    completed_excluded: int

    def with_filters(self, filters: QueueClearFilters) -> QueueClearPreview:
        return QueueClearPreview(
            filters=filters,
            matched=self.matched,
            cancel_immediately=self.cancel_immediately,
            request_interruption=self.request_interruption,
            active_promotions_excluded=self.active_promotions_excluded,
            completed_excluded=self.completed_excluded,
        )


@dataclass(frozen=True, slots=True)
class QueueClearResult:
    changed: int
    operation_id: str


@dataclass(frozen=True, slots=True)
class QueueRetryPreview:
    filters: QueueClearFilters
    matched: int
    retryable: int
    requires_requeue: int
    reset_to_probe: int
    reset_to_plan: int
    reset_to_encode: int
    reset_to_validate: int
    return_to_promote: int

    def with_filters(self, filters: QueueClearFilters) -> QueueRetryPreview:
        return QueueRetryPreview(
            filters=filters,
            matched=self.matched,
            retryable=self.retryable,
            requires_requeue=self.requires_requeue,
            reset_to_probe=self.reset_to_probe,
            reset_to_plan=self.reset_to_plan,
            reset_to_encode=self.reset_to_encode,
            reset_to_validate=self.reset_to_validate,
            return_to_promote=self.return_to_promote,
        )


@dataclass(frozen=True, slots=True)
class QueueRetryResult:
    changed: int
