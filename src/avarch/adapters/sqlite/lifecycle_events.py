from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime

from sqlmodel import Session, select

from avarch.adapters.sqlite.models import JobEvent
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
    ) -> None:
        if dedupe_key is not None:
            existing = self._session.exec(
                select(JobEvent).where(JobEvent.dedupe_key == dedupe_key)
            ).first()
            if existing is not None:
                return

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
