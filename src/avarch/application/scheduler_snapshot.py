from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field

from avarch.domain.jobs import AttemptStatus, JobStage, JobStatus
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


class SchedulerSnapshot(SnapshotModel):
    captured_at: datetime
    workspace: WorkspaceSummary
    scheduler: SchedulerRuntimeSummary
    pipeline: PipelineSummary
    session: object | None = None
    active_jobs: tuple[ActiveJobSummary, ...]
    capacity: CapacitySummary
    resources: object | None = None
    upcoming_jobs: tuple[UpcomingJobSummary, ...] = ()
    recent_events: tuple[object, ...] = ()
    alerts: tuple[SchedulerAlert, ...] = ()
    forecast: object | None = None

    def to_canonical_json(self) -> str:
        return canonical_json(self)


class SchedulerSnapshotQuery(Protocol):
    def snapshot(self) -> SchedulerSnapshot:
        pass
