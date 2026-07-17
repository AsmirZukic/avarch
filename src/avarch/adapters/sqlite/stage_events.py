from __future__ import annotations

from datetime import datetime

from sqlmodel import Session, col, select

from avarch.adapters.sqlite.lifecycle_events import SqliteLifecycleEventStore
from avarch.adapters.sqlite.models import JobAttempt, SchedulerSession
from avarch.domain.jobs import JobEventType, JobStage


def record_stage_event(
    session: Session,
    *,
    job_id: int,
    attempt: JobAttempt,
    event_type: JobEventType,
    now: datetime,
    stage: JobStage | None = None,
    details: dict[str, object | None] | None = None,
) -> None:
    attempt_id = attempt.id
    if attempt_id is None:
        raise RuntimeError("Attempt id was not assigned before recording a stage event.")
    cleaned_details = (
        {key: value for key, value in details.items() if value is not None}
        if details is not None
        else None
    )
    event_stage = JobStage(stage or attempt.stage)
    normalized_event_type = JobEventType(event_type)
    SqliteLifecycleEventStore(session).record_event(
        job_id=job_id,
        attempt_id=attempt_id,
        scheduler_session_id=attempt.scheduler_session_id,
        event_type=normalized_event_type,
        stage=event_stage,
        actor=attempt.runner_id,
        details=cleaned_details,
        dedupe_key=f"attempt:{attempt_id}:{event_stage.value}:{normalized_event_type.value}",
        created_at=now,
    )


def current_scheduler_session_id(session: Session, *, runner_id: str) -> int | None:
    scheduler_session = session.exec(
        select(SchedulerSession)
        .where(
            SchedulerSession.owner_id == runner_id,
            col(SchedulerSession.ended_at).is_(None),
        )
        .order_by(col(SchedulerSession.started_at).desc(), col(SchedulerSession.id).desc())
    ).first()
    return scheduler_session.id if scheduler_session is not None else None
