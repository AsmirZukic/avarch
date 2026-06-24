from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import Column, String, UniqueConstraint
from sqlmodel import Field, SQLModel

from avarch.models.promotion import PromotionMode, PromotionPhase, PromotionStatus
from avarch.models.scheduler import (
    AttemptStatus,
    JobEventType,
    JobOutcomeReason,
    JobStage,
    JobStatus,
    ResourceClass,
    SchedulerMode,
)


class AppMeta(SQLModel, table=True):
    key: str = Field(primary_key=True)
    value: str


class MediaFileStatus(StrEnum):
    ADDED = "added"
    PRESENT = "present"
    CHANGED = "changed"
    MISSING = "missing"


class MediaFile(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    path: str = Field(index=True, unique=True)
    size_bytes: int
    mtime_ns: int
    device_id: int
    inode: int
    fs_fingerprint: str = Field(
        index=True,
        description=(
            "Cheap freshness fingerprint derived from path, size, mtime_ns, device, "
            "and inode. It is not a content hash."
        ),
    )
    discovered_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_seen_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    status: MediaFileStatus = Field(sa_column=Column(String(), nullable=False))
    latest_probe_id: int | None = Field(
        default=None,
        foreign_key="proberesult.id",
        index=True,
    )


class ProbeResult(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    media_file_id: int = Field(foreign_key="mediafile.id", index=True)
    ffprobe_json: str
    normalized_json: str
    probe_hash: str = Field(index=True)
    source_fs_fingerprint: str
    created_at: datetime


class MediaPlan(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)

    media_file_id: int = Field(foreign_key="mediafile.id", index=True)
    probe_result_id: int = Field(foreign_key="proberesult.id", index=True)

    profile_name: str = Field(index=True)
    profile_hash: str = Field(index=True)
    probe_hash: str = Field(index=True)
    source_fs_fingerprint: str = Field(index=True)
    execution_identity_hash: str = Field(index=True)

    plan_hash: str = Field(unique=True, index=True)
    plan_path: str
    output_path: str

    is_current: bool = Field(default=True, index=True)
    is_valid: bool = Field(default=True, index=True)

    created_at: datetime
    superseded_at: datetime | None = None


class Job(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)

    media_file_id: int = Field(foreign_key="mediafile.id", index=True)

    profile_name: str = Field(index=True)
    profile_hash: str

    source_fs_fingerprint: str

    queue_key: str = Field(unique=True, index=True)

    probe_result_id: int | None = Field(
        default=None,
        foreign_key="proberesult.id",
        index=True,
    )
    probe_hash: str | None = None

    plan_hash: str | None = Field(default=None, unique=True, index=True)
    plan_path: str | None = None
    output_path: str | None = None
    latest_validation_id: int | None = Field(
        default=None,
        foreign_key="validationresult.id",
        index=True,
    )
    latest_promotion_id: int | None = Field(
        default=None,
        foreign_key="promotionrecord.id",
        index=True,
    )

    status: JobStatus = Field(sa_column=Column(String(), nullable=False, index=True))
    stage: JobStage = Field(sa_column=Column(String(), nullable=False, index=True))

    priority: int = Field(default=0, index=True)
    attempts: int = 0

    claimed_by: str | None = Field(default=None, index=True)

    last_error_type: str | None = None
    last_error_message: str | None = None
    skip_reason: str | None = None
    outcome_reason: JobOutcomeReason | None = Field(
        default=None,
        sa_column=Column(String(), nullable=True),
    )

    cancel_requested_at: datetime | None = None
    cancel_requested_by: str | None = None
    cancel_reason: str | None = None
    canceled_at: datetime | None = None

    hold_requested_at: datetime | None = None
    hold_requested_by: str | None = None
    hold_reason: str | None = None
    held_at: datetime | None = None

    created_at: datetime
    updated_at: datetime

    started_at: datetime | None = None
    finished_at: datetime | None = None


class JobEvent(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)

    job_id: int = Field(foreign_key="job.id", index=True)

    event_type: JobEventType = Field(sa_column=Column(String(), nullable=False, index=True))

    actor: str
    reason: str | None = None
    details_json: str | None = None

    created_at: datetime


class JobAttempt(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("job_id", "attempt_number"),)

    id: int | None = Field(default=None, primary_key=True)

    job_id: int = Field(foreign_key="job.id", index=True)
    attempt_number: int

    stage: JobStage = Field(sa_column=Column(String(), nullable=False, index=True))
    resource_class: ResourceClass = Field(sa_column=Column(String(), nullable=False, index=True))
    status: AttemptStatus = Field(sa_column=Column(String(), nullable=False, index=True))

    runner_id: str

    command_json: str | None = None
    details_json: str | None = None

    stdout_log: str | None = None
    stderr_log: str | None = None

    temp_dir: str | None = None
    output_path: str | None = None

    exit_code: int | None = None

    error_type: str | None = None
    error_message: str | None = None

    started_at: datetime
    finished_at: datetime | None = None


class ValidationResult(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)

    job_id: int = Field(foreign_key="job.id", index=True)

    attempt_id: int = Field(
        foreign_key="jobattempt.id",
        unique=True,
        index=True,
    )

    plan_hash: str = Field(index=True)
    policy_hash: str = Field(index=True)

    output_path: str

    output_fs_fingerprint: str | None = Field(default=None, index=True)

    passed: bool = Field(index=True)

    details_json: str

    created_at: datetime


class PromotionRecord(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)

    operation_id: str = Field(unique=True, index=True)

    job_id: int = Field(foreign_key="job.id", index=True)
    attempt_id: int = Field(foreign_key="jobattempt.id", unique=True, index=True)
    validation_result_id: int = Field(foreign_key="validationresult.id", index=True)

    mode: PromotionMode = Field(sa_column=Column(String(), nullable=False, index=True))
    status: PromotionStatus = Field(sa_column=Column(String(), nullable=False, index=True))
    phase: PromotionPhase = Field(sa_column=Column(String(), nullable=False, index=True))

    source_path: str
    validated_output_path: str
    final_path: str
    staging_path: str
    backup_path: str | None

    source_fingerprint_before: str
    source_stat_json: str

    validated_output_fingerprint: str
    validated_output_digest: str | None

    staging_digest: str | None

    final_fingerprint: str | None
    final_digest: str | None

    journal_path: str

    owner_token: str | None = Field(default=None, index=True)
    heartbeat_at: datetime | None = None
    lease_expires_at: datetime | None = None

    cleanup_completed: bool = False
    cleanup_error: str | None = None

    error_type: str | None = None
    error_message: str | None = None

    created_at: datetime
    updated_at: datetime

    started_at: datetime
    finished_at: datetime | None = None


class SchedulerState(SQLModel, table=True):
    id: int = Field(default=1, primary_key=True)

    mode: SchedulerMode = Field(
        default=SchedulerMode.RUNNING,
        sa_column=Column(String(), nullable=False, index=True),
    )

    control_generation: int = 0
    acknowledged_generation: int = 0

    control_requested_at: datetime | None = None
    control_acknowledged_at: datetime | None = None

    control_reason: str | None = None

    runner_id: str | None = Field(default=None, index=True)

    heartbeat_at: datetime | None = None
    lease_expires_at: datetime | None = None

    updated_at: datetime
