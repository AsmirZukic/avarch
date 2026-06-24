from __future__ import annotations

from datetime import datetime

from sqlmodel import Session, col, select

from avarch.adapters.sqlite.models import Job, SchedulerState
from avarch.domain.jobs import JobStatus
from avarch.domain.scheduler import SchedulerMode


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