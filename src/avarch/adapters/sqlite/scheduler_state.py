from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlmodel import Session, col, select

from avarch.adapters.sqlite.models import Job, SchedulerState
from avarch.domain.jobs import JobStatus
from avarch.domain.scheduler import SchedulerMode


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


def job_counts_by_status(session: Session) -> dict[JobStatus, int]:
    return {
        status: len(session.exec(select(Job).where(Job.status == status)).all())
        for status in JobStatus
    }


def active_scheduler_jobs(session: Session) -> list[Job]:
    return list(
        session.exec(
            select(Job)
            .where(Job.status == JobStatus.RUNNING)
            .order_by(col(Job.priority).desc(), col(Job.created_at).asc(), col(Job.id).asc())
        ).all()
    )


def pending_cancel_count(session: Session) -> int:
    return len(
        session.exec(
            select(Job).where(
                col(Job.cancel_requested_at).is_not(None),
                Job.status == JobStatus.RUNNING,
            )
        ).all()
    )


def pending_hold_count(session: Session) -> int:
    return len(
        session.exec(
            select(Job).where(
                col(Job.hold_requested_at).is_not(None),
                Job.status == JobStatus.RUNNING,
            )
        ).all()
    )


def has_pending_jobs(session: Session) -> bool:
    return session.exec(select(Job).where(Job.status == JobStatus.PENDING)).first() is not None


def terminal_job_counts(session: Session) -> TerminalJobCounts:
    return TerminalJobCounts(
        completed=len(session.exec(select(Job).where(Job.status == JobStatus.COMPLETED)).all()),
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