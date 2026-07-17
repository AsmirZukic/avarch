from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from avarch.application.resource_telemetry import ResourceHealth, ResourceSample
from avarch.application.scheduler_blockers import JobEligibilityReason
from avarch.domain.jobs import AttemptStatus, JobEventType, JobStage, JobStatus
from avarch.domain.scheduler import SchedulerMode
from avarch.serialization import canonical_json


class SnapshotModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class SchedulerRuntimeState(StrEnum):
    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"
    DRAINING = "draining"
    STOPPING = "stopping"
    STALE = "stale"


class WorkflowStepState(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    COMPLETE = "complete"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class SchedulerAlertSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class WorkspaceSummary(SnapshotModel):
    root_path: str
    database_url: str | None = None


class SchedulerRuntimeSummary(SnapshotModel):
    state: SchedulerRuntimeState
    mode: SchedulerMode | None = None
    owner_id: str | None = None
    pid: int | None = Field(default=None, ge=0, strict=True)
    heartbeat_at: datetime | None = None
    stale: bool = False


class SchedulerSessionRunSummary(SnapshotModel):
    session_id: int = Field(ge=0, strict=True)
    owner_id: str
    workspace_id: str
    pid: int | None = Field(default=None, ge=0, strict=True)
    host: str
    started_at: datetime
    ended_at: datetime | None = None
    end_reason: str | None = None
    active: bool = False


class SessionSummary(SnapshotModel):
    current: SchedulerSessionRunSummary | None = None
    recent: tuple[SchedulerSessionRunSummary, ...] = ()


class PipelineSummary(SnapshotModel):
    queued: int = Field(ge=0, strict=True)
    active: int = Field(ge=0, strict=True)
    encoded: int = Field(default=0, ge=0, strict=True)
    validating: int = Field(default=0, ge=0, strict=True)
    ready_to_promote: int = Field(default=0, ge=0, strict=True)
    promoting: int = Field(default=0, ge=0, strict=True)
    cleaning: int = Field(default=0, ge=0, strict=True)
    completed: int = Field(ge=0, strict=True)
    failed: int = Field(ge=0, strict=True)
    validation_failed: int = Field(default=0, ge=0, strict=True)
    size_rejected: int = Field(default=0, ge=0, strict=True)
    cancelled: int = Field(default=0, ge=0, strict=True)
    held: int = Field(default=0, ge=0, strict=True)


class WorkflowStepSummary(SnapshotModel):
    stage: JobStage
    state: WorkflowStepState


class AttemptProgressSummary(SnapshotModel):
    attempt_id: int = Field(ge=0, strict=True)
    attempt_number: int = Field(ge=1, strict=True)
    status: AttemptStatus
    frames_current: int | None = Field(default=None, ge=0, strict=True)
    frames_total: int | None = Field(default=None, ge=0, strict=True)
    fps: float | None = Field(default=None, ge=0)
    rate_per_second: float | None = Field(default=None, ge=0)
    speed_ratio: float | None = Field(default=None, ge=0)
    eta_seconds: int | None = Field(default=None, ge=0, strict=True)
    elapsed_seconds: int | None = Field(default=None, ge=0, strict=True)
    observed_at: datetime | None = None
    chunks_current: int | None = Field(default=None, ge=0, strict=True)
    chunks_total: int | None = Field(default=None, ge=0, strict=True)
    bitrate_kbps: int | None = Field(default=None, ge=0, strict=True)
    estimated_output_bytes: int | None = Field(default=None, ge=0, strict=True)
    written_output_bytes: int | None = Field(default=None, ge=0, strict=True)
    stale: bool = False
    last_update_age_seconds: int | None = Field(default=None, ge=0, strict=True)


class ActiveJobSummary(SnapshotModel):
    job_id: int = Field(ge=0, strict=True)
    source_path: str
    profile_name: str | None = None
    status: JobStatus
    stage: JobStage
    priority: int = Field(strict=True)
    queued_at: datetime | None = None
    started_at: datetime | None = None
    attempt: AttemptProgressSummary | None = None
    workflow_steps: tuple[WorkflowStepSummary, ...]


class CapacitySummary(SnapshotModel):
    cheap_workers: int = Field(ge=0, strict=True)
    cheap_active: int = Field(default=0, ge=0, strict=True)
    av1an_jobs: int = Field(ge=0, strict=True)
    av1an_active: int = Field(default=0, ge=0, strict=True)
    file_ops: int = Field(ge=0, strict=True)
    file_ops_active: int = Field(default=0, ge=0, strict=True)
    av1an_workers_configured: int | None = Field(default=None, ge=0, strict=True)
    observed_at: datetime | None = None
    stale: bool = False


class UpcomingJobSummary(SnapshotModel):
    job_id: int = Field(ge=0, strict=True)
    source_path: str
    profile_name: str | None = None
    stage: JobStage
    status: JobStatus
    priority: int = Field(strict=True)
    queued_at: datetime | None = None
    selection_position: int | None = Field(default=None, ge=1, strict=True)
    selection_confidence: str | None = None


class SchedulerAlert(SnapshotModel):
    severity: SchedulerAlertSeverity
    code: str
    message: str
    observed_at: datetime | None = None


class BlockedJobSummary(SnapshotModel):
    job_id: int = Field(ge=0, strict=True)
    source_path: str
    profile_name: str | None = None
    stage: JobStage
    status: JobStatus
    reason: JobEligibilityReason
    details: dict[str, object] = Field(default_factory=dict)


class LifecycleEventSummary(SnapshotModel):
    event_id: int = Field(ge=0, strict=True)
    job_id: int = Field(ge=0, strict=True)
    attempt_id: int | None = Field(default=None, ge=0, strict=True)
    scheduler_session_id: int | None = Field(default=None, ge=0, strict=True)
    event_type: JobEventType
    stage: JobStage | None = None
    actor: str
    reason: str | None = None
    details: dict[str, object] = Field(default_factory=dict)
    created_at: datetime


class ResourceMetricSummary(SnapshotModel):
    name: str
    value: float | int | None = None
    total: float | int | None = None
    unit: str | None = None
    available: bool = True
    reason: str | None = None
    health: ResourceHealth = ResourceHealth.OK


class ResourceTelemetrySummary(SnapshotModel):
    sampled_at: datetime
    health: ResourceHealth
    stale: bool = False
    metrics: tuple[ResourceMetricSummary, ...]
    error: str | None = None


class WatchJobDetailsSummary(SnapshotModel):
    job_id: int = Field(ge=0, strict=True)
    attempt_number: int | None = Field(default=None, ge=1, strict=True)
    source_path: str
    profile_name: str | None = None
    status: JobStatus
    stage: JobStage
    started_at: datetime | None = None
    plan_path: str | None = None
    output_path: str | None = None
    stdout_log: str | None = None
    stderr_log: str | None = None
    last_error_type: str | None = None
    last_error_message: str | None = None


class WatchLogTailSummary(SnapshotModel):
    path: str | None = None
    lines: tuple[str, ...] = ()
    missing: bool = False
    truncated: bool = False


class SchedulerSnapshot(SnapshotModel):
    captured_at: datetime
    workspace: WorkspaceSummary
    scheduler: SchedulerRuntimeSummary
    pipeline: PipelineSummary
    session: SessionSummary | None = None
    active_jobs: tuple[ActiveJobSummary, ...]
    capacity: CapacitySummary
    resources: ResourceTelemetrySummary | None = None
    upcoming_jobs: tuple[UpcomingJobSummary, ...] = ()
    blocked_jobs: tuple[BlockedJobSummary, ...] = ()
    recent_events: tuple[LifecycleEventSummary, ...] = ()
    watch_details: WatchJobDetailsSummary | None = None
    watch_log_tail: WatchLogTailSummary | None = None
    alerts: tuple[SchedulerAlert, ...] = ()
    forecast: object | None = None

    def to_canonical_json(self) -> str:
        return canonical_json(self)


class SchedulerSnapshotQuery(Protocol):
    def snapshot(self) -> SchedulerSnapshot:
        ...


def resource_telemetry_summary(
    sample: ResourceSample,
    *,
    captured_at: datetime,
    stale_after_seconds: int,
) -> ResourceTelemetrySummary:
    age_seconds = (
        captured_at.replace(tzinfo=None) - sample.sampled_at.replace(tzinfo=None)
    ).total_seconds()
    return ResourceTelemetrySummary(
        sampled_at=sample.sampled_at,
        health=sample.health,
        stale=age_seconds > stale_after_seconds,
        metrics=tuple(
            ResourceMetricSummary(
                name=metric.name,
                value=metric.value,
                total=metric.total,
                unit=metric.unit,
                available=metric.available,
                reason=metric.reason,
                health=metric.health,
            )
            for metric in sample.metrics
        ),
        error=sample.error,
    )


_WORKFLOW_STAGES = (
    JobStage.PROBE,
    JobStage.PLAN,
    JobStage.ENCODE,
    JobStage.VALIDATE,
    JobStage.PROMOTE,
    JobStage.CLEANUP,
)


def project_workflow_steps(
    *,
    job_status: JobStatus,
    job_stage: JobStage,
    attempt_status: AttemptStatus | None,
) -> tuple[WorkflowStepSummary, ...]:
    terminal_failure_state = _terminal_failure_state(job_status)
    if job_status == JobStatus.PROMOTED:
        return tuple(
            WorkflowStepSummary(
                stage=stage,
                state=(
                    WorkflowStepState.SKIPPED
                    if stage == JobStage.CLEANUP
                    else WorkflowStepState.COMPLETE
                ),
            )
            for stage in _WORKFLOW_STAGES
        )

    if job_status == JobStatus.SIZE_REJECTED:
        return tuple(
            WorkflowStepSummary(stage=stage, state=_size_rejected_step_state(stage))
            for stage in _WORKFLOW_STAGES
        )

    active_stage = _active_stage(job_status, job_stage, attempt_status)
    failed_stage = job_stage if terminal_failure_state is not None else None
    stage_index = _WORKFLOW_STAGES.index(job_stage)

    steps: list[WorkflowStepSummary] = []
    for index, stage in enumerate(_WORKFLOW_STAGES):
        if active_stage == stage:
            state = WorkflowStepState.ACTIVE
        elif failed_stage == stage:
            state = terminal_failure_state
        elif failed_stage is not None and index > stage_index:
            state = WorkflowStepState.BLOCKED
        elif _stage_is_complete(stage, job_status=job_status, job_stage=job_stage):
            state = WorkflowStepState.COMPLETE
        elif stage == JobStage.CLEANUP and job_stage != JobStage.CLEANUP:
            state = WorkflowStepState.SKIPPED
        else:
            state = WorkflowStepState.PENDING
        steps.append(WorkflowStepSummary(stage=stage, state=state))
    return tuple(steps)


def _active_stage(
    job_status: JobStatus,
    job_stage: JobStage,
    attempt_status: AttemptStatus | None,
) -> JobStage | None:
    if attempt_status not in {None, AttemptStatus.RUNNING}:
        return None
    if job_status == JobStatus.ENCODING and job_stage in {
        JobStage.PROBE,
        JobStage.PLAN,
        JobStage.ENCODE,
    }:
        return job_stage
    if job_status == JobStatus.VALIDATING and job_stage == JobStage.VALIDATE:
        return job_stage
    if job_status == JobStatus.PROMOTING and job_stage == JobStage.PROMOTE:
        return job_stage
    if job_status == JobStatus.CLEANING and job_stage == JobStage.CLEANUP:
        return job_stage
    return None


def _terminal_failure_state(job_status: JobStatus) -> WorkflowStepState | None:
    if job_status in {
        JobStatus.FAILED,
        JobStatus.VALIDATION_FAILED,
        JobStatus.CANCELLED,
    }:
        return WorkflowStepState.FAILED
    return None


def _stage_is_complete(stage: JobStage, *, job_status: JobStatus, job_stage: JobStage) -> bool:
    if job_status in {
        JobStatus.ENCODED,
        JobStatus.VALIDATING,
        JobStatus.READY_TO_PROMOTE,
        JobStatus.PROMOTING,
        JobStatus.CLEANING,
    }:
        completed_through = {
            JobStatus.ENCODED: JobStage.ENCODE,
            JobStatus.VALIDATING: JobStage.ENCODE,
            JobStatus.READY_TO_PROMOTE: JobStage.VALIDATE,
            JobStatus.PROMOTING: JobStage.VALIDATE,
            JobStatus.CLEANING: JobStage.PROMOTE,
        }[job_status]
        return _WORKFLOW_STAGES.index(stage) <= _WORKFLOW_STAGES.index(completed_through)
    return _WORKFLOW_STAGES.index(stage) < _WORKFLOW_STAGES.index(job_stage)


def _size_rejected_step_state(stage: JobStage) -> WorkflowStepState:
    if stage in {JobStage.PROBE, JobStage.PLAN, JobStage.ENCODE, JobStage.VALIDATE}:
        return WorkflowStepState.COMPLETE
    if stage == JobStage.PROMOTE:
        return WorkflowStepState.BLOCKED
    return WorkflowStepState.SKIPPED
