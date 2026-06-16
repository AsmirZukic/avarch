from __future__ import annotations

from enum import StrEnum


class SchedulerMode(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    DRAINING = "draining"
    STOPPING = "stopping"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    HELD = "held"
    VALIDATED = "validated"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    SKIPPED = "skipped"


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
