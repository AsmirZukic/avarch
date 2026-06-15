from __future__ import annotations

from enum import StrEnum


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class JobStage(StrEnum):
    PROBE = "probe"
    PLAN = "plan"
    ENCODE = "encode"
    VALIDATE = "validate"


class ResourceClass(StrEnum):
    CHEAP = "cheap"
    HEAVY_AV1AN = "heavy_av1an"
    FILE_OP = "file_op"


class AttemptStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
