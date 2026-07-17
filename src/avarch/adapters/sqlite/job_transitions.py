from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import Engine
from sqlmodel import Session, col, select

from avarch.domain.jobs import (
    AttemptStatus,
    JobEventType,
    JobOutcomeReason,
    JobStage,
    JobStatus,
    JobTransitionError,
    active_status_for_stage,
    job_can_be_claimed_for_stage,
    job_has_passed_validation,
    plan_canceled_transition,
    plan_completed_stage_transition,
    plan_failed_stage_transition,
    plan_interrupted_stage_transition,
    plan_job_transition,
    plan_retry_transition,
    plan_skipped_transition,
    plan_validation_result_transition,
)
from avarch.domain.scheduler import resource_for_stage
from avarch.serialization import canonical_json
from avarch.adapters.sqlite.stage_events import (
    current_scheduler_session_id,
    record_stage_event,
)

if TYPE_CHECKING:
    from avarch.adapters.sqlite.models import Job, JobAttempt

from avarch.adapters.sqlite.models import Job as SQLiteJob
from avarch.adapters.sqlite.models import JobAttempt as SQLiteJobAttempt
from avarch.adapters.sqlite.models import JobEvent

__all__ = [
    "JobClaimError",
    "RecoverySummary",
    "JobTransitionError",
    "add_job_event",
    "attach_probe_and_advance",
    "cancel_claimed_job",
    "claim_job_stage",
    "clear_hold_fields",
    "complete_job_stage",
    "fail_job_stage",
    "fail_job_after_external",
    "interrupt_job_stage",
    "interrupt_running_job",
    "mark_job_skipped",
    "record_validation_result_transition",
    "queue_rejected_output_cleanup",
    "require_attempt",
    "require_job",
    "recover_abandoned_jobs",
    "reset_job_for_retry",
    "skip_claimed_job",
    "transition_job",
]


class JobClaimError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RecoverySummary:
    recovered_jobs: int
    interrupted_attempts: int


def transition_job(
    job: Job,
    target_status: JobStatus,
    reason: JobOutcomeReason | None = None,
    *,
    stage: JobStage | None = None,
    now: datetime | None = None,
) -> None:
    transition = plan_job_transition(job.status, target_status, reason=reason, now=now)
    job.status = transition.status
    if stage is not None:
        job.stage = stage
    if transition.outcome_reason is not None:
        job.outcome_reason = transition.outcome_reason
    if transition.updated_at is not None:
        job.updated_at = transition.updated_at


def claim_job_stage(
    session: Session,
    *,
    job_id: int,
    runner_id: str,
    now: datetime,
) -> JobAttempt:
    job = require_job(session, job_id)
    if not job_can_be_claimed_for_stage(job.status, job.stage):
        raise JobClaimError(f"Job is not claimable: {job_id}")

    transition = plan_job_transition(job.status, active_status_for_stage(job.stage), now=now)
    job.status = transition.status
    job.claimed_by = runner_id
    job.attempts += 1
    job.started_at = job.started_at or now
    job.updated_at = now
    attempt = SQLiteJobAttempt(
        job_id=job_id,
        scheduler_session_id=current_scheduler_session_id(session, runner_id=runner_id),
        attempt_number=job.attempts,
        stage=job.stage,
        resource_class=resource_for_stage(job.stage),
        status=AttemptStatus.RUNNING,
        runner_id=runner_id,
        started_at=now,
    )
    session.add(job)
    session.add(attempt)
    session.flush()
    if attempt.id is None:
        raise JobClaimError("Attempt id was not assigned after claim.")
    record_stage_event(
        session,
        job_id=job_id,
        attempt=attempt,
        event_type=JobEventType.STAGE_STARTED,
        now=now,
    )
    return attempt


def complete_job_stage(
    session: Session,
    *,
    job_id: int,
    attempt_id: int,
    next_stage: JobStage | None,
    now: datetime,
) -> None:
    job = require_job(session, job_id)
    attempt = require_attempt(session, attempt_id)
    if job.cancel_requested_at is not None:
        cancel_claimed_job(session, job=job, attempt=attempt, now=now)
        return

    attempt.status = AttemptStatus.COMPLETED
    attempt.finished_at = now
    job.last_error_type = None
    job.last_error_message = None
    job.claimed_by = None
    if next_stage is None:
        transition = plan_completed_stage_transition(
            job.status,
            attempt.stage,
            next_stage=None,
            hold_requested=False,
            now=now,
        )
    else:
        transition = plan_completed_stage_transition(
            job.status,
            attempt.stage,
            next_stage=next_stage,
            hold_requested=job.hold_requested_at is not None,
            now=now,
        )
    job.status = transition.status
    if transition.stage is not None:
        job.stage = transition.stage
    job.finished_at = transition.finished_at
    job.updated_at = now
    if transition.record_held_event:
        job.held_at = now
        add_job_event(
            session,
            job_id=job_id,
            event_type=JobEventType.HELD,
            actor=job.hold_requested_by or "scheduler",
            reason=job.hold_reason,
            now=now,
        )
    record_stage_event(
        session,
        job_id=job_id,
        attempt=attempt,
        event_type=JobEventType.STAGE_COMPLETED,
        now=now,
    )
    session.add(job)
    session.add(attempt)


def fail_job_stage(
    session: Session,
    *,
    job_id: int,
    attempt_id: int,
    error: Exception,
    exit_code: int | None,
    now: datetime,
) -> None:
    job = require_job(session, job_id)
    attempt = require_attempt(session, attempt_id)
    if job.cancel_requested_at is not None:
        cancel_claimed_job(session, job=job, attempt=attempt, now=now)
        return

    attempt.status = AttemptStatus.FAILED
    attempt.error_type = error.__class__.__name__
    attempt.error_message = str(error)
    attempt.exit_code = exit_code
    attempt.finished_at = now
    transition = plan_failed_stage_transition(job.status, now=now)
    job.status = transition.status
    job.claimed_by = None
    job.last_error_type = attempt.error_type
    job.last_error_message = attempt.error_message
    clear_hold_fields(job)
    job.updated_at = now
    job.finished_at = now
    record_stage_event(
        session,
        job_id=job_id,
        attempt=attempt,
        event_type=JobEventType.STAGE_FAILED,
        now=now,
        details={
            "error_type": attempt.error_type,
            "exit_code": exit_code,
        },
    )
    session.add(job)
    session.add(attempt)


def interrupt_job_stage(
    session: Session,
    *,
    job_id: int,
    attempt_id: int,
    now: datetime,
) -> None:
    job = require_job(session, job_id)
    attempt = require_attempt(session, attempt_id)
    if job.cancel_requested_at is not None:
        cancel_claimed_job(session, job=job, attempt=attempt, now=now)
        return

    attempt.status = AttemptStatus.INTERRUPTED
    attempt.finished_at = now
    attempt.exit_code = 130
    transition = plan_interrupted_stage_transition(
        job.status,
        hold_requested=job.hold_requested_at is not None,
        now=now,
    )
    job.status = transition.status
    if transition.record_held_event:
        job.held_at = now
        add_job_event(
            session,
            job_id=job_id,
            event_type=JobEventType.HELD,
            actor=job.hold_requested_by or "scheduler",
            reason=job.hold_reason,
            now=now,
        )
    job.claimed_by = None
    job.updated_at = now
    job.finished_at = None
    record_stage_event(
        session,
        job_id=job_id,
        attempt=attempt,
        event_type=JobEventType.STAGE_CANCELLED,
        now=now,
        details={"reason": "interrupted"},
    )
    session.add(job)
    session.add(attempt)


def interrupt_running_job(engine: Engine, *, job_id: int, now: datetime) -> None:
    with Session(engine) as session, session.begin():
        job = session.get(SQLiteJob, job_id)
        if job is None or job.status != JobStatus.ENCODING:
            return
        attempt = session.exec(
            select(SQLiteJobAttempt)
            .where(
                SQLiteJobAttempt.job_id == job_id,
                SQLiteJobAttempt.status == AttemptStatus.RUNNING,
            )
            .order_by(col(SQLiteJobAttempt.attempt_number).desc())
        ).first()
        if attempt is None:
            transition = plan_interrupted_stage_transition(
                job.status,
                hold_requested=False,
                now=now,
            )
            job.status = transition.status
            job.claimed_by = None
            job.updated_at = now
            session.add(job)
            return
        interrupt_job_stage(session, job_id=job_id, attempt_id=_require_id(attempt), now=now)


def mark_job_skipped(
    engine: Engine,
    *,
    job_id: int,
    attempt_id: int,
    reason: str,
    now: datetime,
) -> None:
    with Session(engine) as session, session.begin():
        job = require_job(session, job_id)
        skip_claimed_job(session, job=job, attempt_id=attempt_id, reason=reason, now=now)


def queue_rejected_output_cleanup(job: Job, *, now: datetime) -> None:
    transition = plan_job_transition(job.status, JobStatus.QUEUED, now=now)
    job.status = transition.status
    job.stage = JobStage.CLEANUP
    job.claimed_by = None
    job.finished_at = None
    job.updated_at = now


def skip_claimed_job(
    session: Session,
    *,
    job: Job,
    attempt_id: int,
    reason: str,
    now: datetime,
) -> None:
    attempt = require_attempt(session, attempt_id)
    attempt.status = AttemptStatus.COMPLETED
    attempt.finished_at = now
    attempt.details_json = canonical_json({"skip_reason": reason})
    transition = plan_skipped_transition(job.status, reason=None, now=now)
    job.status = transition.status
    job.claimed_by = None
    job.skip_reason = reason
    job.updated_at = now
    job.finished_at = now
    session.add(job)
    session.add(attempt)


def record_validation_result_transition(
    job: Job,
    *,
    passed: bool,
    failed_summary: str,
    now: datetime,
) -> None:
    transition = plan_validation_result_transition(
        job.status,
        passed=passed,
        failed_summary=failed_summary,
        now=now,
    )
    job.status = transition.status
    job.stage = transition.stage
    job.finished_at = transition.finished_at
    if passed:
        job.last_error_type = None
        job.last_error_message = None
    else:
        job.last_error_type = transition.last_error_type
        job.last_error_message = transition.last_error_message


def fail_job_after_external(
    engine: Engine,
    *,
    job_id: int,
    attempt_id: int,
    error: Exception,
    now: datetime,
) -> None:
    with Session(engine) as session, session.begin():
        fail_job_stage(
            session,
            job_id=job_id,
            attempt_id=attempt_id,
            error=error,
            exit_code=None,
            now=now,
        )


def attach_probe_and_advance(
    session: Session,
    *,
    job: Job,
    probe_result_id: int | None,
    probe_hash: str,
    queue_key: str,
    attempt_id: int,
    now: datetime,
) -> None:
    job.queue_key = queue_key
    job.probe_result_id = probe_result_id
    job.probe_hash = probe_hash
    complete_job_stage(
        session,
        job_id=_require_id(job),
        attempt_id=attempt_id,
        next_stage=JobStage.PLAN,
        now=now,
    )


def recover_abandoned_jobs(
    session: Session,
    *,
    now: datetime,
    encoded_output_exists: Callable[[Job], bool],
) -> RecoverySummary:
    recovered_jobs = 0
    interrupted_attempts = 0
    jobs = list(session.exec(select(SQLiteJob).where(SQLiteJob.status == JobStatus.ENCODING)).all())
    jobs.extend(
        session.exec(
            select(SQLiteJob).where(
                col(SQLiteJob.status).in_([JobStatus.ENCODED, JobStatus.VALIDATING])
            )
        ).all()
    )
    jobs.extend(
        session.exec(
            select(SQLiteJob).where(
                col(SQLiteJob.status).in_([JobStatus.READY_TO_PROMOTE, JobStatus.PROMOTING])
            )
        ).all()
    )
    seen_job_ids: set[int] = set()
    for job in jobs:
        job_id = _require_id(job)
        if job_id in seen_job_ids:
            continue
        seen_job_ids.add(job_id)
        if job.status == JobStatus.ENCODED:
            if not encoded_output_exists(job):
                transition = plan_failed_stage_transition(job.status, now=now)
                job.status = transition.status
                job.last_error_type = "SchedulerRecovered"
                job.last_error_message = "Encoded output was missing during scheduler recovery."
                job.claimed_by = None
                job.finished_at = now
                job.updated_at = now
                session.add(job)
                recovered_jobs += 1
                continue
            job.stage = JobStage.VALIDATE
            transition = plan_job_transition(job.status, JobStatus.VALIDATING, now=now)
            job.status = transition.status
            job.claimed_by = None
            job.finished_at = None
            job.updated_at = now
            session.add(job)
            recovered_jobs += 1
            continue
        if job.status == JobStatus.VALIDATING and job.stage == JobStage.VALIDATE:
            attempt = _latest_running_attempt(session, job_id=job_id)
            if attempt is not None:
                attempt.status = AttemptStatus.INTERRUPTED
                attempt.finished_at = now
                attempt.error_type = "SchedulerRecovered"
                attempt.error_message = "Validation attempt was recovered after scheduler restart."
                session.add(attempt)
                interrupted_attempts += 1
            job.claimed_by = None
            job.finished_at = None
            job.updated_at = now
            session.add(job)
            recovered_jobs += 1
            continue
        if job_has_passed_validation(job.status, job.stage):
            job.claimed_by = None
            job.finished_at = None
            job.updated_at = now
            session.add(job)
            recovered_jobs += 1
            continue
        if job.status == JobStatus.PROMOTING and job.stage == JobStage.PROMOTE:
            job.claimed_by = None
            job.finished_at = None
            job.updated_at = now
            session.add(job)
            recovered_jobs += 1
            continue
        attempt = _latest_running_attempt(session, job_id=job_id)
        if attempt is not None and job.cancel_requested_at is not None:
            cancel_claimed_job(session, job=job, attempt=attempt, now=now)
            interrupted_attempts += 1
        elif attempt is not None:
            attempt.status = AttemptStatus.INTERRUPTED
            attempt.finished_at = now
            attempt.error_type = "SchedulerRecovered"
            attempt.error_message = "Running attempt was recovered after scheduler restart."
            session.add(attempt)
            interrupted_attempts += 1
            transition = plan_interrupted_stage_transition(
                job.status,
                hold_requested=job.hold_requested_at is not None,
                now=now,
            )
            job.status = transition.status
            if transition.record_held_event:
                job.held_at = now
                add_job_event(
                    session,
                    job_id=job_id,
                    event_type=JobEventType.HELD,
                    actor=job.hold_requested_by or "scheduler",
                    reason=job.hold_reason,
                    now=now,
                )
            job.claimed_by = None
            job.finished_at = None
            job.updated_at = now
            session.add(job)
        else:
            transition = plan_interrupted_stage_transition(
                job.status,
                hold_requested=job.hold_requested_at is not None,
                now=now,
            )
            job.status = transition.status
            if job.cancel_requested_at is not None:
                transition = plan_canceled_transition(job.status, now=now)
                job.status = transition.status
                job.canceled_at = now
                job.finished_at = now
                add_job_event(
                    session,
                    job_id=job_id,
                    event_type=JobEventType.CANCELED,
                    actor=job.cancel_requested_by or "scheduler",
                    reason=job.cancel_reason,
                    now=now,
                )
            elif transition.record_held_event:
                job.held_at = now
                add_job_event(
                    session,
                    job_id=job_id,
                    event_type=JobEventType.HELD,
                    actor=job.hold_requested_by or "scheduler",
                    reason=job.hold_reason,
                    now=now,
                )
            job.claimed_by = None
            job.updated_at = now
            session.add(job)
        recovered_jobs += 1
    return RecoverySummary(recovered_jobs=recovered_jobs, interrupted_attempts=interrupted_attempts)


def add_job_event(
    session: Session,
    *,
    job_id: int,
    event_type: JobEventType,
    actor: str,
    now: datetime,
    reason: str | None = None,
    details_json: str | None = None,
) -> None:
    session.add(
        JobEvent(
            job_id=job_id,
            event_type=event_type,
            actor=actor,
            reason=reason,
            details_json=details_json,
            created_at=now,
        )
    )


def cancel_claimed_job(
    session: Session,
    *,
    job: Job,
    attempt: JobAttempt,
    now: datetime,
) -> None:
    job_id = _require_id(job)
    attempt.status = AttemptStatus.CANCELED
    attempt.finished_at = now
    attempt.exit_code = 130
    transition = plan_canceled_transition(job.status, now=now)
    job.status = transition.status
    job.claimed_by = None
    job.canceled_at = now
    job.finished_at = now
    job.updated_at = now
    clear_hold_fields(job)
    add_job_event(
        session,
        job_id=job_id,
        event_type=JobEventType.CANCELED,
        actor=job.cancel_requested_by or "scheduler",
        reason=job.cancel_reason,
        now=now,
    )
    record_stage_event(
        session,
        job_id=job_id,
        attempt=attempt,
        event_type=JobEventType.STAGE_CANCELLED,
        now=now,
        details={"reason": "cancelled"},
    )
    session.add(job)
    session.add(attempt)


def clear_hold_fields(job: Job) -> None:
    job.hold_requested_at = None
    job.hold_requested_by = None
    job.hold_reason = None
    job.held_at = None


def clear_cancel_fields(job: Job) -> None:
    job.cancel_requested_at = None
    job.cancel_requested_by = None
    job.cancel_reason = None
    job.canceled_at = None


def reset_job_for_retry(job: Job, *, next_stage: JobStage, now: datetime) -> None:
    clear_cancel_fields(job)
    clear_hold_fields(job)
    transition = plan_retry_transition(next_stage)
    job.status = transition.status
    job.stage = transition.stage
    job.last_error_type = None
    job.last_error_message = None
    job.claimed_by = None
    job.finished_at = None
    job.updated_at = now


def require_job(session: Session, job_id: int) -> Job:
    job = session.get(SQLiteJob, job_id)
    if job is None:
        raise JobClaimError(f"Job not found: {job_id}")
    return job


def require_attempt(session: Session, attempt_id: int) -> JobAttempt:
    attempt = session.get(SQLiteJobAttempt, attempt_id)
    if attempt is None:
        raise JobClaimError(f"Job attempt not found: {attempt_id}")
    return attempt


def _latest_running_attempt(session: Session, *, job_id: int) -> JobAttempt | None:
    return session.exec(
        select(SQLiteJobAttempt)
        .where(SQLiteJobAttempt.job_id == job_id, SQLiteJobAttempt.status == AttemptStatus.RUNNING)
        .order_by(col(SQLiteJobAttempt.attempt_number).desc())
    ).first()


def _require_id(value: object) -> int:
    identifier = getattr(value, "id", None)
    if not isinstance(identifier, int):
        raise JobClaimError("Expected a persisted row id.")
    return identifier
