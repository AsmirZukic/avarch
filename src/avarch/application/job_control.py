from __future__ import annotations

from datetime import datetime
from typing import Protocol


class JobControlWorkflowError(RuntimeError):
    pass


class JobControlStore(Protocol):
    def running_job_ids(self) -> list[int]: ...

    def cancel_job(
        self,
        *,
        job_id: int,
        actor: str,
        now: datetime,
        reason: str | None,
    ) -> None: ...

    def hold_job(
        self,
        *,
        job_id: int,
        actor: str,
        now: datetime,
        reason: str | None,
    ) -> None: ...

    def release_job(self, *, job_id: int, actor: str, now: datetime) -> bool: ...

    def update_job_priority(
        self,
        *,
        job_id: int,
        priority: int,
        actor: str,
        now: datetime,
    ) -> None: ...


def cancel_jobs(
    store: JobControlStore,
    *,
    job_id: int | None,
    running: bool,
    actor: str,
    now: datetime,
    reason: str | None = None,
) -> int:
    if running:
        running_ids = store.running_job_ids()
        for running_job_id in running_ids:
            store.cancel_job(
                job_id=running_job_id,
                actor=actor,
                reason=reason,
                now=now,
            )
        return len(running_ids)
    if job_id is None:
        raise JobControlWorkflowError("Provide JOB_ID or --running.")
    store.cancel_job(job_id=job_id, actor=actor, reason=reason, now=now)
    return 1


def hold_job(
    store: JobControlStore,
    *,
    job_id: int,
    actor: str,
    now: datetime,
    reason: str | None = None,
) -> None:
    store.hold_job(job_id=job_id, actor=actor, reason=reason, now=now)


def release_job(store: JobControlStore, *, job_id: int, actor: str, now: datetime) -> bool:
    return store.release_job(job_id=job_id, actor=actor, now=now)


def update_job_priority(
    store: JobControlStore,
    *,
    job_id: int,
    priority: int,
    actor: str,
    now: datetime,
) -> None:
    store.update_job_priority(job_id=job_id, priority=priority, actor=actor, now=now)
