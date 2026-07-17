from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from avarch.domain.jobs import JobEventType, JobStage


@dataclass(frozen=True, slots=True)
class LifecycleEventRecord:
    id: int
    job_id: int
    attempt_id: int | None
    scheduler_session_id: int | None
    event_type: JobEventType
    stage: JobStage | None
    actor: str
    reason: str | None
    details: Mapping[str, object]
    dedupe_key: str | None
    created_at: datetime


class LifecycleEventStore(Protocol):
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
        pass

    def latest_events(self, *, limit: int) -> tuple[LifecycleEventRecord, ...]:
        pass
