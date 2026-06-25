from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from avarch.domain.jobs import JobEventType, JobStage, JobStatus, job_can_retry
from avarch.domain.scheduler import (
    QueueClearAction,
    RetryFacts,
    classify_queue_clear_job,
    select_retry_stage,
)
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


@dataclass(frozen=True, slots=True)
class QueueRetrySummary:
    matched: int
    retryable: int
    requires_requeue: int
    reset_to_probe: int
    reset_to_plan: int
    reset_to_encode: int
    reset_to_validate: int
    return_to_promote: int


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


class QueueRetryStore(QueueControlStore, Protocol):
    def retry_facts(self, *, job_id: int) -> RetryFacts: ...

    def request_job_retry(
        self,
        *,
        job_id: int,
        next_stage: JobStage,
        actor: str,
        now: datetime,
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


def retry_job(
    store: QueueRetryStore,
    *,
    job_id: int,
    actor: str,
    now: datetime,
) -> JobStage:
    jobs = store.select_queue_jobs(
        job_ids={job_id},
        statuses=None,
        stages=None,
        profile=None,
        all_jobs=False,
    )
    job = jobs[0] if jobs else None
    if job is None:
        raise QueueControlError(f"Job not found: {job_id}")
    if not job_can_retry(job.status):
        raise QueueControlError(f"Job {job_id} cannot be retried from {job.status}.")
    next_stage = select_retry_stage(store.retry_facts(job_id=job_id))
    if next_stage is None:
        raise QueueControlError("Job already has a completed promotion.")
    store.request_job_retry(
        job_id=job_id,
        next_stage=next_stage,
        actor=actor,
        now=now,
    )
    return next_stage


def retry_queue(
    store: QueueRetryStore,
    *,
    actor: str,
    now: datetime,
    job_ids: set[int] | None = None,
    statuses: set[JobStatus] | None = None,
    stages: set[JobStage] | None = None,
    profile: str | None = None,
    all_jobs: bool = False,
    confirm: bool = False,
) -> QueueRetrySummary:
    jobs = store.select_queue_jobs(
        job_ids=job_ids,
        statuses=statuses,
        stages=stages,
        profile=profile,
        all_jobs=all_jobs,
    )
    retryable = 0
    requires_requeue = 0
    reset_to_probe = 0
    reset_to_plan = 0
    reset_to_encode = 0
    reset_to_validate = 0
    return_to_promote = 0
    for job in jobs:
        if not job_can_retry(job.status):
            continue
        try:
            next_stage = select_retry_stage(store.retry_facts(job_id=job.job_id))
        except QueueControlError:
            requires_requeue += 1
            continue
        if next_stage is None:
            requires_requeue += 1
            continue
        retryable += 1
        if next_stage == JobStage.PROBE:
            reset_to_probe += 1
        elif next_stage == JobStage.PLAN:
            reset_to_plan += 1
        elif next_stage == JobStage.ENCODE:
            reset_to_encode += 1
        elif next_stage == JobStage.VALIDATE:
            reset_to_validate += 1
        elif next_stage == JobStage.PROMOTE:
            return_to_promote += 1
        if confirm:
            store.request_job_retry(
                job_id=job.job_id,
                next_stage=next_stage,
                actor=actor,
                now=now,
            )
    return QueueRetrySummary(
        matched=len(jobs),
        retryable=retryable,
        requires_requeue=requires_requeue,
        reset_to_probe=reset_to_probe,
        reset_to_plan=reset_to_plan,
        reset_to_encode=reset_to_encode,
        reset_to_validate=reset_to_validate,
        return_to_promote=return_to_promote,
    )
