from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from avarch.domain.jobs import AttemptStatus, JobEventType, JobOutcomeReason, JobStage, JobStatus
from avarch.domain.progress import ProgressSnapshot


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
class ResourceDecisionView:
    mode: str
    effective_workers: str
    effective_svt_lp: str
    reason: str
    confidence: float
    algorithm_version: int
    fallback: bool
    evidence_count: int | None = None


@dataclass(frozen=True, slots=True)
class PerformanceObservationView:
    schema_version: int
    total_frames: int | None
    observation_duration_seconds: float | None
    aggregate_fps: float | None
    peak_rss_bytes: int | None
    peak_cgroup_memory_bytes: int | None
    average_cpu_utilization_percent: float | None
    cpu_throttled_events_delta: int | None
    cpu_throttled_usec_delta: int | None
    memory_oom_events_delta: int | None
    memory_oom_kill_events_delta: int | None
    incomplete: bool
    resource_policy_hash: str | None


@dataclass(frozen=True, slots=True)
class JobAttemptView:
    attempt_number: int
    stage: JobStage
    status: AttemptStatus
    runner_id: str
    stdout_log: str | None
    stderr_log: str | None
    resource_decision: ResourceDecisionView | None = None
    performance: PerformanceObservationView | None = None


@dataclass(frozen=True, slots=True)
class CurrentJobProgressView:
    job_id: int
    job_status: JobStatus
    job_stage: JobStage
    attempt_id: int | None
    attempt: JobAttemptView | None
    progress: ProgressSnapshot | None


@dataclass(frozen=True, slots=True)
class WorkflowJobItem:
    id: int | None
    status: JobStatus
    stage: JobStage
    output_path: str | None
    plan_hash: str | None


@dataclass(frozen=True, slots=True)
class RecentFailedJob:
    id: int | None
    stage: JobStage
    file_name: str
    last_error_type: str | None
    last_error_message: str | None
    latest_attempt: JobAttemptView | None


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

    def workflow_jobs_for_plan_hashes(
        self,
        *,
        plan_hashes: tuple[str, ...],
    ) -> list[WorkflowJobItem]: ...

    def recent_failed_jobs(self, *, limit: int) -> list[RecentFailedJob]: ...

    def latest_attempt(
        self,
        *,
        job_id: int,
        attempt_number: int | None,
    ) -> JobAttemptView | None: ...

    def current_job_progress(self, *, job_id: int) -> CurrentJobProgressView | None: ...


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


def workflow_jobs_for_plan_hashes(
    store: JobViewStore,
    *,
    plan_hashes: tuple[str, ...],
) -> list[WorkflowJobItem]:
    return store.workflow_jobs_for_plan_hashes(plan_hashes=plan_hashes)


def recent_failed_jobs(store: JobViewStore, *, limit: int) -> list[RecentFailedJob]:
    return store.recent_failed_jobs(limit=limit)


def latest_attempt(
    store: JobViewStore,
    *,
    job_id: int,
    attempt_number: int | None,
) -> JobAttemptView | None:
    return store.latest_attempt(job_id=job_id, attempt_number=attempt_number)


def current_job_progress(
    store: JobViewStore,
    *,
    job_id: int,
) -> CurrentJobProgressView | None:
    return store.current_job_progress(job_id=job_id)
