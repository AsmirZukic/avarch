from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from avarch.domain.jobs import (
    JobOutcomeReason,
    JobStatus,
    JobTransitionError,
    plan_job_transition,
)

if TYPE_CHECKING:
    from avarch.adapters.sqlite.models import Job

__all__ = ["JobTransitionError", "transition_job"]


def transition_job(
    job: Job,
    target_status: JobStatus,
    reason: JobOutcomeReason | None = None,
    *,
    now: datetime | None = None,
) -> None:
    transition = plan_job_transition(job.status, target_status, reason=reason, now=now)
    job.status = transition.status
    if transition.outcome_reason is not None:
        job.outcome_reason = transition.outcome_reason
    if transition.updated_at is not None:
        job.updated_at = transition.updated_at
