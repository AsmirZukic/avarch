from __future__ import annotations

from datetime import datetime

from sqlmodel import Session

from avarch.adapters.sqlite import job_control
from avarch.adapters.sqlite.queue import QueueSelectionError, select_queue_jobs
from avarch.application.queue_control import QueueControlError, QueueJobSnapshot
from avarch.domain.jobs import JobEventType, JobStage, JobStatus


class SqliteQueueControlStore:
    def __init__(self, session: Session) -> None:
        self._session = session

    def select_queue_jobs(
        self,
        *,
        job_ids: set[int] | None,
        statuses: set[JobStatus] | None,
        stages: set[JobStage] | None,
        profile: str | None,
        all_jobs: bool,
    ) -> list[QueueJobSnapshot]:
        try:
            rows = select_queue_jobs(
                self._session,
                job_ids=job_ids,
                statuses=statuses,
                stages=stages,
                profile=profile,
                all_jobs=all_jobs,
            )
        except QueueSelectionError as exc:
            raise QueueControlError(str(exc)) from exc
        return [
            QueueJobSnapshot(
                job_id=_require_id(row),
                status=JobStatus(row.status),
                stage=JobStage(row.stage),
            )
            for row in rows
        ]

    def cancel_job(
        self,
        *,
        job_id: int,
        actor: str,
        now: datetime,
        reason: str,
        event_type: JobEventType,
        details_json: str,
    ) -> None:
        try:
            job_control.cancel_job(
                self._session,
                job_id=job_id,
                actor=actor,
                now=now,
                reason=reason,
                event_type=event_type,
                details_json=details_json,
            )
        except job_control.JobControlError as exc:
            raise QueueControlError(str(exc)) from exc


def _require_id(value: object) -> int:
    identifier = getattr(value, "id", None)
    if not isinstance(identifier, int):
        raise QueueControlError("Expected a persisted row id.")
    return identifier
