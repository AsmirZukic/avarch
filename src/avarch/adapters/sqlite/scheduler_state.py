from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlmodel import Session, col, select

from avarch.adapters.sqlite.models import Job, SchedulerState
from avarch.domain.jobs import JobStatus
from avarch.domain.scheduler import SchedulerMode

SCHEDULER_LEASE_SECONDS = 30.0


class SchedulerAlreadyRunningError(RuntimeError):
    pass


class SchedulerLeaseLostError(RuntimeError):
    pass


class SchedulerControlError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TerminalJobCounts:
    completed: int
    failed: int
    skipped: int


def get_or_create_scheduler_state(session: Session, *, now: datetime) -> SchedulerState:
    state = session.get(SchedulerState, 1)
    if state is None:
        state = SchedulerState(id=1, mode=SchedulerMode.RUNNING, updated_at=now)
        session.add(state)
        session.flush()
    return state


def acquire_scheduler_lease(
    session: Session,
    *,
    runner_id: str,
    now: datetime,
    resume: bool = False,
) -> SchedulerState:
    state = get_or_create_scheduler_state(session, now=now)
    if (
        state.runner_id is not None
        and state.lease_expires_at is not None
        and datetime_after(state.lease_expires_at, now)
        and state.runner_id != runner_id
    ):
        raise SchedulerAlreadyRunningError("Another scheduler lease is still active.")

    if resume and state.mode == SchedulerMode.PAUSED:
        request_scheduler_mode(
            state,
            mode=SchedulerMode.RUNNING,
            now=now,
            reason=None,
            increment=True,
        )
    elif state.mode == SchedulerMode.PAUSED:
        raise SchedulerControlError("Scheduler is paused. Run `avarch scheduler resume` first.")

    if state.mode in {SchedulerMode.DRAINING, SchedulerMode.STOPPING} and not lease_active(
        state,
        now=now,
    ):
        state.mode = SchedulerMode.RUNNING
        state.control_requested_at = None
        state.control_acknowledged_at = None
        state.control_reason = None

    state.runner_id = runner_id
    state.heartbeat_at = now
    state.lease_expires_at = now + timedelta(seconds=SCHEDULER_LEASE_SECONDS)
    state.updated_at = now
    session.add(state)
    return state


def renew_scheduler_lease(
    session: Session,
    *,
    runner_id: str,
    now: datetime,
) -> None:
    state = get_or_create_scheduler_state(session, now=now)
    if state.runner_id != runner_id:
        raise SchedulerLeaseLostError("Scheduler lease belongs to another runner.")
    state.heartbeat_at = now
    state.lease_expires_at = now + timedelta(seconds=SCHEDULER_LEASE_SECONDS)
    state.updated_at = now
    session.add(state)


def release_scheduler_lease(
    session: Session,
    *,
    runner_id: str,
    now: datetime,
) -> None:
    state = get_or_create_scheduler_state(session, now=now)
    if state.runner_id != runner_id:
        return
    state.runner_id = None
    state.lease_expires_at = None
    state.heartbeat_at = now
    if state.mode in {SchedulerMode.DRAINING, SchedulerMode.STOPPING}:
        state.mode = SchedulerMode.RUNNING
        state.control_requested_at = None
        state.control_acknowledged_at = None
        state.control_reason = None
    state.updated_at = now
    session.add(state)


def acknowledge_scheduler_control(
    session: Session,
    *,
    runner_id: str,
    now: datetime,
) -> None:
    state = get_or_create_scheduler_state(session, now=now)
    if state.runner_id != runner_id:
        return
    state.acknowledged_generation = state.control_generation
    state.control_acknowledged_at = now
    session.add(state)


def pause_scheduler(session: Session, *, now: datetime, reason: str | None = None) -> None:
    state = get_or_create_scheduler_state(session, now=now)
    if state.mode == SchedulerMode.PAUSED:
        return
    if state.mode != SchedulerMode.RUNNING:
        raise SchedulerControlError(f"Cannot pause while scheduler is {state.mode}.")
    request_scheduler_mode(state, mode=SchedulerMode.PAUSED, now=now, reason=reason)
    session.add(state)


def resume_scheduler(session: Session, *, now: datetime) -> None:
    state = get_or_create_scheduler_state(session, now=now)
    if state.mode == SchedulerMode.RUNNING:
        return
    if state.mode != SchedulerMode.PAUSED:
        raise SchedulerControlError(f"Cannot resume while scheduler is {state.mode}.")
    request_scheduler_mode(state, mode=SchedulerMode.RUNNING, now=now, reason=None)
    session.add(state)


def drain_scheduler(session: Session, *, now: datetime, reason: str | None = None) -> None:
    state = get_or_create_scheduler_state(session, now=now)
    if not lease_active(state, now=now):
        raise SchedulerControlError("No active scheduler lease is running.")
    if state.mode != SchedulerMode.RUNNING:
        raise SchedulerControlError(f"Cannot drain while scheduler is {state.mode}.")
    request_scheduler_mode(state, mode=SchedulerMode.DRAINING, now=now, reason=reason)
    session.add(state)


def stop_scheduler(session: Session, *, now: datetime, reason: str | None = None) -> None:
    state = get_or_create_scheduler_state(session, now=now)
    if not lease_active(state, now=now):
        raise SchedulerControlError("No active scheduler lease is running.")
    if state.mode not in {SchedulerMode.RUNNING, SchedulerMode.PAUSED}:
        raise SchedulerControlError(f"Cannot stop while scheduler is {state.mode}.")
    request_scheduler_mode(state, mode=SchedulerMode.STOPPING, now=now, reason=reason)
    session.add(state)


def request_scheduler_mode(
    state: SchedulerState,
    *,
    mode: SchedulerMode,
    now: datetime,
    reason: str | None,
    increment: bool = True,
) -> None:
    state.mode = mode
    if increment:
        state.control_generation += 1
    state.control_requested_at = now
    state.control_acknowledged_at = None
    state.control_reason = reason
    state.updated_at = now


def lease_active(state: SchedulerState, *, now: datetime) -> bool:
    return (
        state.runner_id is not None
        and state.lease_expires_at is not None
        and datetime_after(state.lease_expires_at, now)
    )


def datetime_after(left: datetime, right: datetime) -> bool:
    return left.replace(tzinfo=None) > right.replace(tzinfo=None)


def job_counts_by_status(session: Session) -> dict[JobStatus, int]:
    return {
        status: len(session.exec(select(Job).where(Job.status == status)).all())
        for status in JobStatus
    }


def active_scheduler_jobs(session: Session) -> list[Job]:
    return list(
        session.exec(
            select(Job)
            .where(Job.status == JobStatus.ENCODING)
            .order_by(col(Job.priority).desc(), col(Job.created_at).asc(), col(Job.id).asc())
        ).all()
    )


def pending_cancel_count(session: Session) -> int:
    return len(
        session.exec(
            select(Job).where(
                col(Job.cancel_requested_at).is_not(None),
                Job.status == JobStatus.ENCODING,
            )
        ).all()
    )


def pending_hold_count(session: Session) -> int:
    return len(
        session.exec(
            select(Job).where(
                col(Job.hold_requested_at).is_not(None),
                Job.status == JobStatus.ENCODING,
            )
        ).all()
    )


def has_pending_jobs(session: Session) -> bool:
    return session.exec(select(Job).where(Job.status == JobStatus.QUEUED)).first() is not None


def terminal_job_counts(session: Session) -> TerminalJobCounts:
    return TerminalJobCounts(
        completed=len(session.exec(select(Job).where(Job.status == JobStatus.PROMOTED)).all()),
        failed=len(session.exec(select(Job).where(Job.status == JobStatus.FAILED)).all()),
        skipped=len(session.exec(select(Job).where(Job.status == JobStatus.SKIPPED)).all()),
    )


def active_jobs_with_cancel_requested(session: Session, *, job_ids: set[int]) -> set[int]:
    if not job_ids:
        return set()
    return {
        job_id
        for job_id in job_ids
        if (job := session.get(Job, job_id)) is not None and job.cancel_requested_at is not None
    }
