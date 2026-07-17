from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime

from sqlmodel import Session, col, select

from avarch.adapters.sqlite.models import JobEvent
from avarch.application.lifecycle_events import LifecycleEventRecord
from avarch.domain.jobs import JobEventType, JobStage
from avarch.serialization import canonical_json


class SqliteLifecycleEventStore:
    def __init__(self, session: Session) -> None:
        self._session = session

    def record_event(
        self,
        *,
        job_id: int,
        event_type: JobEventType,
        actor: str,
        created_at: datetime,
        attempt_id: int | None = None,
        scheduler_session_id: int | None = None,
        stage: JobStage | None = None,
        reason: str | None = None,
        details: Mapping[str, object] | None = None,
        dedupe_key: str | None = None,
    ) -> LifecycleEventRecord:
        if dedupe_key is not None:
            existing = self._session.exec(
                select(JobEvent).where(JobEvent.dedupe_key == dedupe_key)
            ).first()
            if existing is not None:
                return _record(existing)

        event = JobEvent(
            job_id=job_id,
            attempt_id=attempt_id,
            scheduler_session_id=scheduler_session_id,
            event_type=event_type,
            stage=stage,
            actor=actor,
            reason=reason,
            details_json=canonical_json(dict(details)) if details is not None else None,
            dedupe_key=dedupe_key,
            created_at=created_at,
        )
        self._session.add(event)
        self._session.flush()
        return _record(event)

    def latest_events(self, *, limit: int) -> tuple[LifecycleEventRecord, ...]:
        if limit <= 0:
            return ()
        events = self._session.exec(
            select(JobEvent)
            .order_by(col(JobEvent.created_at).desc(), col(JobEvent.id).desc())
            .limit(limit)
        ).all()
        return tuple(_record(event) for event in events)


def _record(event: JobEvent) -> LifecycleEventRecord:
    event_id = event.id
    if event_id is None:
        raise RuntimeError("Lifecycle event was not persisted.")
    return LifecycleEventRecord(
        id=event_id,
        job_id=event.job_id,
        attempt_id=event.attempt_id,
        scheduler_session_id=event.scheduler_session_id,
        event_type=JobEventType(event.event_type),
        stage=JobStage(event.stage) if event.stage is not None else None,
        actor=event.actor,
        reason=event.reason,
        details=_details(event.details_json),
        dedupe_key=event.dedupe_key,
        created_at=event.created_at,
    )


def _details(details_json: str | None) -> Mapping[str, object]:
    if details_json is None:
        return {}
    value = json.loads(details_json)
    if isinstance(value, dict):
        return value
    return {"value": value}
