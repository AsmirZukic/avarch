from __future__ import annotations

from enum import StrEnum


class SchedulerMode(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    DRAINING = "draining"
    STOPPING = "stopping"


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
