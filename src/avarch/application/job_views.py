from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from avarch.domain.jobs import AttemptStatus, JobEventType, JobOutcomeReason, JobStage, JobStatus


@dataclass(frozen=True, slots=True)
class JobListItem:
    id: int | None
    status: JobStatus
    stage: JobStage
    priority: int
    attempts: int
    profile_name: str
    file_name: str
    cancel_requested_at: datetime | None
    hold_requested_at: datetime | None
    last_error_message: str | None
    skip_reason: str | None
    outcome_reason: JobOutcomeReason | None


@dataclass(frozen=True, slots=True)
class JobAttemptView:
    attempt_number: int
    stage: JobStage
    status: AttemptStatus
    runner_id: str
    stdout_log: str | None
    stderr_log: str | None


@dataclass(frozen=True, slots=True)
class JobEventView:
    id: int | None
    created_at: datetime
    event_type: JobEventType
    actor: str


@dataclass(frozen=True, slots=True)
class JobDetails:
    id: int | None
    source_path: str
    profile_name: str
    profile_hash: str
    queue_key: str
    priority: int
    status: JobStatus
    stage: JobStage
    claimed_by: str | None
    attempts: int
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    cancel_requested_at: datetime | None
    hold_requested_at: datetime | None
    probe_hash: str | None
    plan_hash: str | None
    plan_path: str | None
    output_path: str | None
    latest_validation_id: int | None
    latest_promotion_id: int | None
    last_error_type: str | None
    last_error_message: str | None
    attempt_history: list[JobAttemptView]
    events: list[JobEventView]


class JobViewStore(Protocol):
    def list_jobs(
        self,
        *,
        statuses: set[JobStatus] | None,
        stages: set[JobStage] | None,
        profile: str | None,
        limit: int | None,
    ) -> list[JobListItem]: ...

    def job_details(self, *, job_id: int) -> JobDetails | None: ...

    def latest_attempt(
        self,
        *,
        job_id: int,
        attempt_number: int | None,
    ) -> JobAttemptView | None: ...


def list_jobs(
    store: JobViewStore,
    *,
    statuses: set[JobStatus] | None,
    stages: set[JobStage] | None,
    profile: str | None,
    limit: int | None,
) -> list[JobListItem]:
    return store.list_jobs(statuses=statuses, stages=stages, profile=profile, limit=limit)


def job_details(store: JobViewStore, *, job_id: int) -> JobDetails | None:
    return store.job_details(job_id=job_id)


def latest_attempt(
    store: JobViewStore,
    *,
    job_id: int,
    attempt_number: int | None,
) -> JobAttemptView | None:
    return store.latest_attempt(job_id=job_id, attempt_number=attempt_number)
