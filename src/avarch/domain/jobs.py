from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class JobStatus(StrEnum):
    QUEUED = "queued"
    ENCODING = "encoding"
    ENCODED = "encoded"
    VALIDATING = "validating"
    VALIDATION_FAILED = "validation_failed"
    SIZE_REJECTED = "size_rejected"
    READY_TO_PROMOTE = "ready_to_promote"
    PROMOTING = "promoting"
    PROMOTED = "promoted"
    SKIPPED = "skipped"
    FAILED = "failed"
    CANCELLED = "cancelled"

    PENDING = "queued"
    RUNNING = "encoding"
    HELD = "held"
    VALIDATED = "ready_to_promote"
    COMPLETED = "promoted"
    CANCELED = "cancelled"

    @classmethod
    def _missing_(cls, value: object) -> JobStatus | None:
        legacy = {
            "pending": cls.QUEUED,
            "running": cls.ENCODING,
            "validated": cls.READY_TO_PROMOTE,
            "completed": cls.PROMOTED,
            "canceled": cls.CANCELLED,
        }
        return legacy.get(value) if isinstance(value, str) else None


class JobOutcomeReason(StrEnum):
    SUCCESS = "success"
    SKIPPED_SIZE_NOT_SMALLER = "skipped_size_not_smaller"
    SKIPPED_MINIMUM_SAVINGS_NOT_MET = "skipped_minimum_savings_not_met"
    FAILED_VALIDATION = "failed_validation"
    FAILED_PROMOTION = "failed_promotion"
    FAILED_ENCODING = "failed_encoding"
    CANCELLED_BY_USER = "cancelled_by_user"


class JobStage(StrEnum):
    PROBE = "probe"
    PLAN = "plan"
    ENCODE = "encode"
    VALIDATE = "validate"
    PROMOTE = "promote"


class ResourceClass(StrEnum):
    CHEAP = "cheap"
    HEAVY_AV1AN = "heavy_av1an"
    FILE_OP = "file_op"


class AttemptStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELED = "canceled"


class JobEventType(StrEnum):
    HOLD_REQUESTED = "hold_requested"
    HELD = "held"
    HOLD_RELEASED = "hold_released"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELED = "canceled"
    RETRY_REQUESTED = "retry_requested"
    PRIORITY_CHANGED = "priority_changed"
    QUEUE_CLEARED = "queue_cleared"


class InterruptionReason(StrEnum):
    USER_CANCEL = "user_cancel"
    SCHEDULER_STOP = "scheduler_stop"
    LEASE_LOST = "lease_lost"
    CTRL_C = "ctrl_c"


class JobTransitionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class JobTransition:
    status: JobStatus
    outcome_reason: JobOutcomeReason | None = None
    updated_at: datetime | None = None


def active_status_for_stage(stage: JobStage | str) -> JobStatus:
    normalized_stage = JobStage(stage)
    if normalized_stage == JobStage.VALIDATE:
        return JobStatus.VALIDATING
    if normalized_stage == JobStage.PROMOTE:
        return JobStatus.PROMOTING
    return JobStatus.ENCODING


def job_can_be_claimed_for_stage(status: JobStatus | str, stage: JobStage | str) -> bool:
    normalized_status = JobStatus(status)
    normalized_stage = JobStage(stage)
    if normalized_status == JobStatus.QUEUED:
        return True
    return normalized_stage == JobStage.VALIDATE and normalized_status in {
        JobStatus.ENCODED,
        JobStatus.VALIDATING,
    }


_ALLOWED_TRANSITIONS: Mapping[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset(
        {
            JobStatus.QUEUED,
            JobStatus.ENCODING,
            JobStatus.VALIDATING,
            JobStatus.READY_TO_PROMOTE,
            JobStatus.SKIPPED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.HELD,
        }
    ),
    JobStatus.ENCODING: frozenset(
        {
            JobStatus.ENCODING,
            JobStatus.ENCODED,
            JobStatus.QUEUED,
            JobStatus.HELD,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.ENCODED: frozenset(
        {
            JobStatus.ENCODED,
            JobStatus.VALIDATING,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.VALIDATING: frozenset(
        {
            JobStatus.VALIDATING,
            JobStatus.READY_TO_PROMOTE,
            JobStatus.VALIDATION_FAILED,
            JobStatus.SIZE_REJECTED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.VALIDATION_FAILED: frozenset({JobStatus.VALIDATION_FAILED, JobStatus.QUEUED}),
    JobStatus.SIZE_REJECTED: frozenset({JobStatus.SIZE_REJECTED, JobStatus.QUEUED}),
    JobStatus.READY_TO_PROMOTE: frozenset(
        {
            JobStatus.READY_TO_PROMOTE,
            JobStatus.PROMOTING,
            JobStatus.SIZE_REJECTED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.PROMOTING: frozenset(
        {
            JobStatus.PROMOTING,
            JobStatus.PROMOTED,
            JobStatus.READY_TO_PROMOTE,
            JobStatus.FAILED,
        }
    ),
    JobStatus.PROMOTED: frozenset({JobStatus.PROMOTED}),
    JobStatus.SKIPPED: frozenset({JobStatus.SKIPPED, JobStatus.QUEUED}),
    JobStatus.FAILED: frozenset(
        {
            JobStatus.FAILED,
            JobStatus.QUEUED,
            JobStatus.VALIDATING,
            JobStatus.READY_TO_PROMOTE,
        }
    ),
    JobStatus.CANCELLED: frozenset({JobStatus.CANCELLED, JobStatus.QUEUED}),
    JobStatus.HELD: frozenset({JobStatus.HELD, JobStatus.QUEUED, JobStatus.CANCELLED}),
}


def plan_job_transition(
    current_status: JobStatus | str,
    target_status: JobStatus | str,
    reason: JobOutcomeReason | str | None = None,
    *,
    now: datetime | None = None,
) -> JobTransition:
    normalized_current = JobStatus(current_status)
    normalized_target = JobStatus(target_status)
    allowed = _ALLOWED_TRANSITIONS.get(normalized_current, frozenset())
    if normalized_target not in allowed:
        raise JobTransitionError(
            f"Invalid job transition: {normalized_current.value} -> {normalized_target.value}"
        )
    normalized_reason = JobOutcomeReason(reason) if reason is not None else None
    return JobTransition(
        status=normalized_target,
        outcome_reason=normalized_reason,
        updated_at=now,
    )