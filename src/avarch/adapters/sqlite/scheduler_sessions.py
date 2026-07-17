from __future__ import annotations

from datetime import datetime

from sqlmodel import Session, col, select

from avarch.adapters.sqlite.models import SchedulerSession


def start_scheduler_session(
    session: Session,
    *,
    owner_id: str,
    workspace_id: str,
    pid: int | None,
    host: str,
    now: datetime,
) -> SchedulerSession:
    _mark_interrupted_open_sessions(
        session,
        owner_id=owner_id,
        workspace_id=workspace_id,
        now=now,
    )
    existing = session.exec(
        select(SchedulerSession).where(
            SchedulerSession.owner_id == owner_id,
            SchedulerSession.workspace_id == workspace_id,
            col(SchedulerSession.ended_at).is_(None),
        )
    ).first()
    if existing is not None:
        return existing
    scheduler_session = SchedulerSession(
        owner_id=owner_id,
        workspace_id=workspace_id,
        pid=pid,
        host=host,
        started_at=now,
    )
    session.add(scheduler_session)
    session.flush()
    return scheduler_session


def end_scheduler_session(
    session: Session,
    *,
    session_id: int,
    now: datetime,
    reason: str,
) -> None:
    scheduler_session = session.get(SchedulerSession, session_id)
    if scheduler_session is None or scheduler_session.ended_at is not None:
        return
    scheduler_session.ended_at = now
    scheduler_session.end_reason = reason
    session.add(scheduler_session)


def current_scheduler_session(
    session: Session,
    *,
    workspace_id: str,
) -> SchedulerSession | None:
    return session.exec(
        select(SchedulerSession)
        .where(
            SchedulerSession.workspace_id == workspace_id,
            col(SchedulerSession.ended_at).is_(None),
        )
        .order_by(col(SchedulerSession.started_at).desc(), col(SchedulerSession.id).desc())
    ).first()


def _mark_interrupted_open_sessions(
    session: Session,
    *,
    owner_id: str,
    workspace_id: str,
    now: datetime,
) -> None:
    open_sessions = session.exec(
        select(SchedulerSession).where(
            SchedulerSession.workspace_id == workspace_id,
            col(SchedulerSession.ended_at).is_(None),
            SchedulerSession.owner_id != owner_id,
        )
    ).all()
    for scheduler_session in open_sessions:
        scheduler_session.ended_at = now
        scheduler_session.end_reason = "interrupted"
        session.add(scheduler_session)
