from __future__ import annotations

from datetime import datetime

from sqlmodel import Session, select

from avarch.adapters.sqlite import job_control
from avarch.adapters.sqlite.models import Job
from avarch.application.job_control import JobControlWorkflowError
from avarch.domain.jobs import JobStatus


class SqliteJobControlStore:
    def __init__(self, session: Session) -> None:
        self._session = session

    def running_job_ids(self) -> list[int]:
        jobs = self._session.exec(select(Job).where(Job.status == JobStatus.ENCODING)).all()
        return [job.id for job in jobs if job.id is not None]

    def cancel_job(
        self,
        *,
        job_id: int,
        actor: str,
        now: datetime,
        reason: str | None,
    ) -> None:
        try:
            job_control.cancel_job(
                self._session,
                job_id=job_id,
                actor=actor,
                reason=reason,
                now=now,
            )
        except job_control.JobControlError as exc:
            raise JobControlWorkflowError(str(exc)) from exc

    def hold_job(
        self,
        *,
        job_id: int,
        actor: str,
        now: datetime,
        reason: str | None,
    ) -> None:
        try:
            job_control.hold_job(
                self._session,
                job_id=job_id,
                actor=actor,
                reason=reason,
                now=now,
            )
        except job_control.JobControlError as exc:
            raise JobControlWorkflowError(str(exc)) from exc

    def release_job(self, *, job_id: int, actor: str, now: datetime) -> bool:
        try:
            return job_control.release_job(self._session, job_id=job_id, actor=actor, now=now)
        except job_control.JobControlError as exc:
            raise JobControlWorkflowError(str(exc)) from exc

    def update_job_priority(
        self,
        *,
        job_id: int,
        priority: int,
        actor: str,
        now: datetime,
    ) -> None:
        try:
            job_control.update_job_priority(
                self._session,
                job_id=job_id,
                priority=priority,
                actor=actor,
                now=now,
            )
        except job_control.JobControlError as exc:
            raise JobControlWorkflowError(str(exc)) from exc
