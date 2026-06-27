from __future__ import annotations

from datetime import datetime
from pathlib import Path

from avarch.adapters.sqlite.job_transitions import transition_job
from avarch.adapters.sqlite.models import Job
from avarch.domain.jobs import JobOutcomeReason, JobStatus
from avarch.domain.size import SizeDecision
from avarch.profiles.models import EncodingProfile


class RejectedOutputCleanupError(RuntimeError):
    pass


def cleanup_rejected_output(
    job: Job,
    *,
    encoded_path: Path,
    decision: SizeDecision,
    profile: EncodingProfile,
    now: datetime,
) -> None:
    if decision == SizeDecision.ACCEPT:
        raise ValueError("Accepted outputs must not be cleaned up as rejected.")
    try:
        if profile.promotion.delete_rejected_output:
            encoded_path.unlink(missing_ok=True)
    except OSError as exc:
        transition_job(job, JobStatus.FAILED, reason=JobOutcomeReason.FAILED_PROMOTION, now=now)
        job.last_error_type = exc.__class__.__name__
        job.last_error_message = f"Unable to delete rejected output: {encoded_path}"
        job.finished_at = now
        raise RejectedOutputCleanupError(job.last_error_message) from exc

    reason = (
        JobOutcomeReason.SKIPPED_SIZE_NOT_SMALLER
        if decision == SizeDecision.REJECT_NOT_SMALLER
        else JobOutcomeReason.SKIPPED_MINIMUM_SAVINGS_NOT_MET
    )
    transition_job(job, JobStatus.SIZE_REJECTED, reason=reason, now=now)
    job.claimed_by = None
    job.finished_at = now
