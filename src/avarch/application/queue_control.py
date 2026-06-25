from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from avarch.domain.jobs import JobEventType, JobStage, JobStatus
from avarch.domain.scheduler import QueueClearAction, classify_queue_clear_job
from avarch.serialization import canonical_json


class QueueControlError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class QueueJobSnapshot:
    job_id: int
    status: JobStatus
    stage: JobStage


@dataclass(frozen=True, slots=True)
class QueueClearSummary:
    matched: int
    immediate_cancel: int
    running_requests: int
    promotion_excluded: int
    completed_excluded: int
    changed: int
    operation_id: str


class QueueControlStore(Protocol):
    def select_queue_jobs(
        self,
        *,
        job_ids: set[int] | None,
        statuses: set[JobStatus] | None,
        stages: set[JobStage] | None,
        profile: str | None,
        all_jobs: bool,
    ) -> list[QueueJobSnapshot]: ...

    def cancel_job(
        self,
        *,
        job_id: int,
        actor: str,
        now: datetime,
        reason: str,
        event_type: JobEventType,
        details_json: str,
    ) -> None: ...


def clear_queue(
    store: QueueControlStore,
    *,
    actor: str,
    now: datetime,
    job_ids: set[int] | None = None,
    statuses: set[JobStatus] | None = None,
    stages: set[JobStage] | None = None,
    profile: str | None = None,
    all_jobs: bool = False,
    cancel_running: bool = False,
    confirm: bool = False,
) -> QueueClearSummary:
    jobs = store.select_queue_jobs(
        job_ids=job_ids,
        statuses=statuses,
        stages=stages,
        profile=profile,
        all_jobs=all_jobs,
    )
    operation_id = uuid.uuid4().hex
    immediate = 0
    running = 0
    promotion_excluded = 0
    completed_excluded = 0
    changed = 0
    for job in jobs:
        action = classify_queue_clear_job(
            job.status,
            job.stage,
            cancel_running=cancel_running,
        )
        if action == QueueClearAction.EXCLUDE_TERMINAL:
            completed_excluded += 1
            continue
        if action == QueueClearAction.EXCLUDE_RUNNING_PROMOTION:
            promotion_excluded += 1
            continue
        if action == QueueClearAction.SKIP_RUNNING:
            continue
        if action == QueueClearAction.REQUEST_RUNNING_CANCEL:
            running += 1
        else:
            immediate += 1
        if confirm:
            store.cancel_job(
                job_id=job.job_id,
                actor=actor,
                now=now,
                reason="queue clear",
                event_type=JobEventType.QUEUE_CLEARED,
                details_json=canonical_json({"operation_id": operation_id}),
            )
            changed += 1
    return QueueClearSummary(
        matched=len(jobs),
        immediate_cancel=immediate,
        running_requests=running,
        promotion_excluded=promotion_excluded,
        completed_excluded=completed_excluded,
        changed=changed,
        operation_id=operation_id,
    )
