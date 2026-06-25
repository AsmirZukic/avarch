from __future__ import annotations

from datetime import datetime

from sqlmodel import Session

from avarch.adapters.sqlite.job_transitions import (
    add_job_event,
    clear_hold_fields,
    require_job,
    reset_job_for_retry,
)
from avarch.adapters.sqlite.models import Job
from avarch.domain.jobs import JobEventType, JobStage, JobStatus
from avarch.serialization import canonical_json

__all__ = [
    "JobControlError",
    "cancel_job",
    "hold_job",
    "release_job",
    "request_job_retry",
    "reset_retry_job",
    "update_job_priority",
]


class JobControlError(RuntimeError):
    pass


def cancel_job(
    session: Session,
    *,
    job_id: int,
    actor: str,
    now: datetime,
    reason: str | None = None,
    event_type: JobEventType = JobEventType.CANCEL_REQUESTED,
    details_json: str | None = None,
) -> None:
    job = require_job(session, job_id)
    if job.status == JobStatus.CANCELED:
        return
    if job.status in {JobStatus.COMPLETED, JobStatus.SKIPPED}:
        raise JobControlError(f"Job {job_id} cannot be canceled from {job.status}.")
    if job.status == JobStatus.RUNNING and job.stage == JobStage.PROMOTE:
        raise JobControlError(
            "This job has an active promotion transaction. Use promotion recovery or interrupt "
            "the owning promote command."
        )

    job.cancel_requested_at = job.cancel_requested_at or now
    job.cancel_requested_by = job.cancel_requested_by or actor
    job.cancel_reason = reason
    if job.status == JobStatus.RUNNING:
        add_job_event(
            session,
            job_id=job_id,
            event_type=event_type,
            actor=actor,
            reason=reason,
            details_json=details_json,
            now=now,
        )
    else:
        job.status = JobStatus.CANCELED
        job.canceled_at = now
        job.finished_at = now
        job.claimed_by = None
        clear_hold_fields(job)
        add_job_event(
            session,
            job_id=job_id,
            event_type=JobEventType.CANCELED
            if event_type == JobEventType.CANCEL_REQUESTED
            else event_type,
            actor=actor,
            reason=reason,
            details_json=details_json,
            now=now,
        )
    job.updated_at = now
    session.add(job)


def hold_job(
    session: Session,
    *,
    job_id: int,
    actor: str,
    now: datetime,
    reason: str | None = None,
) -> None:
    job = require_job(session, job_id)
    if job.status == JobStatus.HELD:
        return
    if job.status == JobStatus.RUNNING and job.stage == JobStage.PROMOTE:
        raise JobControlError("Running promotion jobs cannot be held by the scheduler.")
    if job.status in {
        JobStatus.FAILED,
        JobStatus.CANCELED,
        JobStatus.COMPLETED,
        JobStatus.SKIPPED,
        JobStatus.VALIDATED,
    }:
        raise JobControlError(f"Job {job_id} cannot be held from {job.status}.")

    job.hold_requested_at = job.hold_requested_at or now
    job.hold_requested_by = job.hold_requested_by or actor
    job.hold_reason = reason
    if job.status == JobStatus.RUNNING:
        add_job_event(
            session,
            job_id=job_id,
            event_type=JobEventType.HOLD_REQUESTED,
            actor=actor,
            reason=reason,
            now=now,
        )
    else:
        job.status = JobStatus.HELD
        job.held_at = now
        add_job_event(
            session,
            job_id=job_id,
            event_type=JobEventType.HELD,
            actor=actor,
            reason=reason,
            now=now,
        )
    job.updated_at = now
    session.add(job)


def release_job(session: Session, *, job_id: int, actor: str, now: datetime) -> bool:
    job = require_job(session, job_id)
    had_hold = job.hold_requested_at is not None or job.status == JobStatus.HELD
    if not had_hold:
        return False
    if job.status == JobStatus.HELD:
        job.status = JobStatus.PENDING
        job.finished_at = None
    clear_hold_fields(job)
    job.updated_at = now
    add_job_event(
        session,
        job_id=job_id,
        event_type=JobEventType.HOLD_RELEASED,
        actor=actor,
        now=now,
    )
    session.add(job)
    return True


def reset_retry_job(
    session: Session,
    *,
    job: Job,
    next_stage: JobStage,
    now: datetime,
) -> None:
    reset_job_for_retry(job, next_stage=next_stage, now=now)
    session.add(job)


def request_job_retry(
    session: Session,
    *,
    job: Job,
    next_stage: JobStage,
    actor: str,
    now: datetime,
) -> None:
    reset_retry_job(session, job=job, next_stage=next_stage, now=now)
    add_job_event(
        session,
        job_id=_require_id(job),
        event_type=JobEventType.RETRY_REQUESTED,
        actor=actor,
        now=now,
    )


def update_job_priority(
    session: Session,
    *,
    job_id: int,
    priority: int,
    actor: str,
    now: datetime,
) -> None:
    job = require_job(session, job_id)
    if job.status not in {JobStatus.PENDING, JobStatus.HELD}:
        raise JobControlError(f"Job {job_id} priority cannot change from {job.status}.")
    old_priority = job.priority
    job.priority = priority
    job.updated_at = now
    add_job_event(
        session,
        job_id=job_id,
        event_type=JobEventType.PRIORITY_CHANGED,
        actor=actor,
        details_json=canonical_json({"old_priority": old_priority, "new_priority": priority}),
        now=now,
    )
    session.add(job)


def _require_id(value: object) -> int:
    identifier = getattr(value, "id", None)
    if not isinstance(identifier, int):
        raise JobControlError("Expected a persisted row id.")
    return identifier