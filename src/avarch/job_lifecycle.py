from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING

from avarch.models.scheduler import JobOutcomeReason, JobStatus

if TYPE_CHECKING:
    from avarch.models.db import Job


class JobTransitionError(ValueError):
    pass


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


def transition_job(
    job: Job,
    target_status: JobStatus,
    reason: JobOutcomeReason | None = None,
    *,
    now: datetime | None = None,
) -> None:
    current_status = JobStatus(job.status)
    normalized_target = JobStatus(target_status)
    allowed = _ALLOWED_TRANSITIONS.get(current_status, frozenset())
    if normalized_target not in allowed:
        raise JobTransitionError(
            f"Invalid job transition: {current_status.value} -> {normalized_target.value}"
        )
    job.status = normalized_target
    if reason is not None:
        job.outcome_reason = reason
    if now is not None:
        job.updated_at = now
