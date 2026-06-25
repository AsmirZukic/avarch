from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session

from avarch.adapters.sqlite import job_transitions as job_transition_adapter
from avarch.adapters.sqlite import scheduler_state as scheduler_state_adapter
from avarch.adapters.sqlite.db import create_db_engine
from avarch.adapters.sqlite.job_transitions import interrupt_running_job
from avarch.adapters.sqlite.models import Job
from avarch.adapters.sqlite.queue import claimable_jobs
from avarch.adapters.sqlite.scheduler_state import (
    active_jobs_with_cancel_requested,
    active_scheduler_jobs,
    get_or_create_scheduler_state,
    has_pending_jobs,
    job_counts_by_status,
    pending_cancel_count,
    pending_hold_count,
    terminal_job_counts,
)
from avarch.config import AppConfig
from avarch.domain.jobs import JobStage, JobStatus
from avarch.domain.scheduler import (
    ActiveJob,
    ClaimableJob,
    ResourceCapacity,
    SchedulerMode,
    jobs_to_cancel,
    scheduler_can_launch_jobs,
    select_launchable_jobs,
)
from avarch.scheduler_support import encoded_output_exists, require_id
from avarch.scheduler_workers import (
    execute_encode_job,
    execute_plan_job,
    execute_probe_job,
    execute_promotion_job,
    execute_validation_job,
)

SCHEDULER_HEARTBEAT_SECONDS = 5.0
SCHEDULER_POLL_SECONDS = 1.0
SCHEDULER_IDLE_EXIT_SECONDS = 2.0
SCHEDULER_CONTROL_POLL_SECONDS = 0.5

JobWorker = Callable[..., Coroutine[Any, Any, Any]]


@dataclass(frozen=True, slots=True)
class SchedulerRunSummary:
    completed: int
    failed: int
    skipped: int
    idle: bool


@dataclass(frozen=True, slots=True)
class SchedulerStatus:
    mode: SchedulerMode
    control_generation: int
    acknowledged_generation: int
    runner_id: str | None
    heartbeat_at: datetime | None
    lease_expires_at: datetime | None
    lease_state: str
    counts_by_status: dict[JobStatus, int]
    cancel_pending: int
    hold_pending: int
    active_jobs: list[Job]


def utc_now() -> datetime:
    return datetime.now(UTC)


async def run_scheduler(
    *,
    config: AppConfig,
    runner_id: str,
    resume: bool = False,
) -> SchedulerRunSummary:
    engine = create_db_engine(config.database.url)
    with Session(engine) as session, session.begin():
        scheduler_state_adapter.acquire_scheduler_lease(
            session,
            runner_id=runner_id,
            now=utc_now(),
            resume=resume,
        )
        job_transition_adapter.recover_abandoned_jobs(
            session,
            now=utc_now(),
            encoded_output_exists=encoded_output_exists,
        )

    workers = _scheduler_workers()
    active: dict[asyncio.Task[Any], tuple[int, JobStage]] = {}
    active_job_ids: set[int] = set()
    last_heartbeat = utc_now()
    idle_since: datetime | None = None
    current_mode = SchedulerMode.RUNNING
    resource_capacity = _resource_capacity(config)

    try:
        while True:
            now = utc_now()
            if (now - last_heartbeat).total_seconds() >= SCHEDULER_HEARTBEAT_SECONDS:
                with Session(engine) as session, session.begin():
                    scheduler_state_adapter.renew_scheduler_lease(
                        session,
                        runner_id=runner_id,
                        now=now,
                    )
                last_heartbeat = now

            done = [task for task in active if task.done()]
            for task in done:
                job_id, _stage = active.pop(task)
                active_job_ids.discard(job_id)
                try:
                    task.result()
                except asyncio.CancelledError:
                    interrupt_running_job(engine, job_id=job_id, now=utc_now())

            with Session(engine) as session:
                state = get_or_create_scheduler_state(session, now=now)
                if state.runner_id != runner_id:
                    raise scheduler_state_adapter.SchedulerLeaseLostError(
                        "Scheduler lease belongs to another runner."
                    )
                current_mode = SchedulerMode(state.mode)
                if state.acknowledged_generation != state.control_generation:
                    with Session(engine) as ack_session, ack_session.begin():
                        scheduler_state_adapter.acknowledge_scheduler_control(
                            ack_session,
                            runner_id=runner_id,
                            now=utc_now(),
                        )

                active_jobs_snapshot = _active_jobs(active)
                requested_cancel_ids = active_jobs_with_cancel_requested(
                    session,
                    job_ids=active_job_ids,
                )
                cancel_ids = jobs_to_cancel(
                    active_jobs_snapshot,
                    cancel_requested_job_ids=requested_cancel_ids,
                    mode=current_mode,
                )
                if cancel_ids:
                    _cancel_active_tasks(active, cancel_ids)

                if scheduler_can_launch_jobs(current_mode):
                    _launch_claimable_jobs(
                        session,
                        active=active,
                        active_job_ids=active_job_ids,
                        workers=workers,
                        runner_id=runner_id,
                        config=config,
                        resource_capacity=resource_capacity,
                        active_jobs_snapshot=active_jobs_snapshot,
                    )

            if active:
                await asyncio.sleep(SCHEDULER_CONTROL_POLL_SECONDS)
                continue

            if current_mode in {SchedulerMode.DRAINING, SchedulerMode.STOPPING}:
                break

            if current_mode == SchedulerMode.PAUSED:
                await asyncio.sleep(SCHEDULER_CONTROL_POLL_SECONDS)
                continue

            with Session(engine) as session:
                pending = has_pending_jobs(session)
            if not pending:
                idle_since = idle_since or utc_now()
                if (utc_now() - idle_since).total_seconds() >= SCHEDULER_IDLE_EXIT_SECONDS:
                    break
            else:
                idle_since = None
            await asyncio.sleep(SCHEDULER_POLL_SECONDS)
    finally:
        with Session(engine) as session, session.begin():
            scheduler_state_adapter.release_scheduler_lease(
                session,
                runner_id=runner_id,
                now=utc_now(),
            )

    with Session(engine) as session:
        terminal_counts = terminal_job_counts(session)
    return SchedulerRunSummary(
        completed=terminal_counts.completed,
        failed=terminal_counts.failed,
        skipped=terminal_counts.skipped,
        idle=True,
    )


def scheduler_status(session: Session, *, now: datetime) -> SchedulerStatus:
    state = get_or_create_scheduler_state(session, now=now)
    counts_by_status = job_counts_by_status(session)
    active_jobs = active_scheduler_jobs(session)
    cancel_pending = pending_cancel_count(session)
    hold_pending = pending_hold_count(session)
    lease_state = "inactive"
    if state.runner_id is not None:
        lease_state = "active" if scheduler_state_adapter.lease_active(state, now=now) else "stale"
    return SchedulerStatus(
        mode=SchedulerMode(state.mode),
        control_generation=state.control_generation,
        acknowledged_generation=state.acknowledged_generation,
        runner_id=state.runner_id,
        heartbeat_at=state.heartbeat_at,
        lease_expires_at=state.lease_expires_at,
        lease_state=lease_state,
        counts_by_status=counts_by_status,
        cancel_pending=cancel_pending,
        hold_pending=hold_pending,
        active_jobs=active_jobs,
    )


def _scheduler_workers() -> dict[JobStage, JobWorker]:
    return {
        JobStage.PROBE: execute_probe_job,
        JobStage.PLAN: execute_plan_job,
        JobStage.ENCODE: execute_encode_job,
        JobStage.VALIDATE: execute_validation_job,
        JobStage.PROMOTE: execute_promotion_job,
    }


def _resource_capacity(config: AppConfig) -> ResourceCapacity:
    return ResourceCapacity(
        cheap_workers=config.resources.cheap_workers,
        av1an_jobs=config.resources.av1an_jobs,
        file_ops=config.resources.file_ops,
    )


def _active_jobs(active: dict[asyncio.Task[Any], tuple[int, JobStage]]) -> list[ActiveJob]:
    return [ActiveJob(job_id=job_id, stage=stage) for job_id, stage in active.values()]


def _cancel_active_tasks(
    active: dict[asyncio.Task[Any], tuple[int, JobStage]],
    job_ids: set[int] | frozenset[int],
) -> None:
    for task, (job_id, _stage) in active.items():
        if job_id in job_ids:
            task.cancel()


def _launch_claimable_jobs(
    session: Session,
    *,
    active: dict[asyncio.Task[Any], tuple[int, JobStage]],
    active_job_ids: set[int],
    workers: dict[JobStage, JobWorker],
    runner_id: str,
    config: AppConfig,
    resource_capacity: ResourceCapacity,
    active_jobs_snapshot: list[ActiveJob],
) -> None:
    claimable = [
        ClaimableJob(job_id=require_id(job), stage=job.stage)
        for job in claimable_jobs(session, active_job_ids=active_job_ids)
    ]
    launchable = select_launchable_jobs(
        claimable,
        active_jobs=active_jobs_snapshot,
        capacity=resource_capacity,
    )
    for job in launchable:
        worker = workers[job.stage]
        task = asyncio.create_task(worker(job_id=job.job_id, runner_id=runner_id, config=config))
        active[task] = (job.job_id, job.stage)
        active_job_ids.add(job.job_id)
