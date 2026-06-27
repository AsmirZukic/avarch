from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from avarch.domain.jobs import JobStage, JobStatus
from avarch.domain.scheduler import SchedulerMode


@dataclass(frozen=True, slots=True)
class ActiveSchedulerJob:
    job_id: int | None
    stage: JobStage
    file_name: str


@dataclass(frozen=True, slots=True)
class SchedulerStatusView:
    mode: SchedulerMode
    runner_id: str | None
    lease_state: str
    counts_by_status: dict[JobStatus, int]
    cancel_pending: int
    hold_pending: int
    active_jobs: list[ActiveSchedulerJob]


class SchedulerStatusStore(Protocol):
    def scheduler_status(self, *, now: datetime) -> SchedulerStatusView: ...


def scheduler_status(store: SchedulerStatusStore, *, now: datetime) -> SchedulerStatusView:
    return store.scheduler_status(now=now)
