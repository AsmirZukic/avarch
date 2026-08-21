from __future__ import annotations

import asyncio
import os
import socket
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from avarch.config import AppConfig
from avarch.domain.jobs import JobStage
from avarch.domain.scheduler import (
    ActiveJob,
    ClaimableJob,
    ResourceCapacity,
    SchedulerMode,
    jobs_to_cancel,
    resource_for_stage,
    scheduler_can_launch_jobs,
    select_launchable_jobs,
)

MAX_CLI_LOG_TAIL_BYTES = 64 * 1024
SCHEDULER_HEARTBEAT_SECONDS = 5.0
SCHEDULER_POLL_SECONDS = 1.0
SCHEDULER_IDLE_EXIT_SECONDS = 2.0
SCHEDULER_CONTROL_POLL_SECONDS = 0.5


@dataclass(frozen=True, slots=True)
class SchedulerRunSummary:
    completed: int
    failed: int
    skipped: int
    idle: bool


@dataclass(frozen=True, slots=True)
class SchedulerControlSnapshot:
    mode: SchedulerMode
    control_generation: int
    acknowledged_generation: int
    runner_id: str | None


@dataclass(frozen=True, slots=True)
class SchedulerTerminalCounts:
    completed: int
    failed: int
    skipped: int


@dataclass(frozen=True, slots=True)
class SchedulerCapacityUsage:
    cheap_workers: int
    cheap_active: int
    av1an_jobs: int
    av1an_active: int
    file_ops: int
    file_ops_active: int


class SchedulerRunStore(Protocol):
    def acquire_lease(
        self,
        *,
        runner_id: str,
        now: datetime,
        resume: bool,
        capacity: SchedulerCapacityUsage,
    ) -> None: ...

    def start_session(self, *, runner_id: str, now: datetime) -> int: ...

    def recover_abandoned_jobs(self, *, now: datetime) -> None: ...

    def renew_lease(
        self,
        *,
        runner_id: str,
        now: datetime,
        capacity: SchedulerCapacityUsage,
    ) -> None: ...

    def load_control_snapshot(self, *, now: datetime) -> SchedulerControlSnapshot: ...

    def acknowledge_control(self, *, runner_id: str, now: datetime) -> None: ...

    def cancel_requested_job_ids(self, *, job_ids: set[int]) -> set[int]: ...

    def claimable_jobs(self, *, active_job_ids: set[int]) -> list[ClaimableJob]: ...

    def has_pending_jobs(self) -> bool: ...

    def interrupt_running_job(self, *, job_id: int, now: datetime) -> None: ...

    def end_session(self, *, session_id: int, now: datetime, reason: str) -> None: ...

    def release_lease(self, *, runner_id: str, now: datetime) -> None: ...

    def terminal_counts(self) -> SchedulerTerminalCounts: ...


class SchedulerWorkerRegistry(Protocol):
    async def run_job(
        self,
        *,
        stage: JobStage,
        job_id: int,
        runner_id: str,
        config: AppConfig,
    ) -> None: ...

    def pause_active_jobs(self) -> None: ...

    def resume_active_jobs(self) -> None: ...


class SchedulerRuntime(Protocol):
    def store(self, *, config: AppConfig) -> SchedulerRunStore: ...

    def workers(self) -> SchedulerWorkerRegistry: ...


class SchedulerRunError(RuntimeError):
    pass


class SchedulerAlreadyRunningError(SchedulerRunError):
    pass


class SchedulerControlError(SchedulerRunError):
    pass


class SchedulerLeaseLostError(SchedulerRunError):
    pass


def utc_now() -> datetime:
    return datetime.now(UTC)


async def run_scheduler(
    runtime: SchedulerRuntime,
    *,
    config: AppConfig,
    runner_id: str,
    resume: bool = False,
) -> SchedulerRunSummary:
    store = runtime.store(config=config)
    resource_capacity = _resource_capacity(config)
    active: dict[asyncio.Task[Any], ActiveJob] = {}
    active_job_ids: set[int] = set()
    store.acquire_lease(
        runner_id=runner_id,
        now=utc_now(),
        resume=resume,
        capacity=_capacity_usage(resource_capacity, _active_jobs(active)),
    )
    session_id: int | None = None
    end_reason = "normal"
    workers = runtime.workers()
    workers_paused = False

    try:
        session_id = store.start_session(runner_id=runner_id, now=utc_now())
        initial_terminal_counts = store.terminal_counts()
        store.recover_abandoned_jobs(now=utc_now())

        last_heartbeat = utc_now()
        idle_since: datetime | None = None
        current_mode = SchedulerMode.RUNNING

        while True:
            now = utc_now()
            if (now - last_heartbeat).total_seconds() >= SCHEDULER_HEARTBEAT_SECONDS:
                store.renew_lease(
                    runner_id=runner_id,
                    now=now,
                    capacity=_capacity_usage(resource_capacity, _active_jobs(active)),
                )
                last_heartbeat = now

            done = [task for task in active if task.done()]
            for task in done:
                job = active.pop(task)
                active_job_ids.discard(job.job_id)
                try:
                    task.result()
                except asyncio.CancelledError:
                    store.interrupt_running_job(job_id=job.job_id, now=utc_now())
                    raise
            if done:
                store.renew_lease(
                    runner_id=runner_id,
                    now=utc_now(),
                    capacity=_capacity_usage(resource_capacity, _active_jobs(active)),
                )
                last_heartbeat = utc_now()

            snapshot = store.load_control_snapshot(now=now)
            if snapshot.runner_id != runner_id:
                raise SchedulerLeaseLostError("Scheduler lease belongs to another runner.")
            current_mode = snapshot.mode
            if current_mode == SchedulerMode.PAUSED and not workers_paused:
                workers.pause_active_jobs()
                workers_paused = True
            elif current_mode != SchedulerMode.PAUSED and workers_paused:
                workers.resume_active_jobs()
                workers_paused = False
            if snapshot.acknowledged_generation != snapshot.control_generation:
                store.acknowledge_control(runner_id=runner_id, now=utc_now())

            active_jobs_snapshot = _active_jobs(active)
            cancel_ids = jobs_to_cancel(
                active_jobs_snapshot,
                cancel_requested_job_ids=store.cancel_requested_job_ids(job_ids=active_job_ids),
                mode=current_mode,
            )
            if cancel_ids:
                if workers_paused:
                    workers.resume_active_jobs()
                    workers_paused = False
                _cancel_active_tasks(active, cancel_ids)

            if scheduler_can_launch_jobs(current_mode):
                before_launch = len(active)
                _launch_claimable_jobs(
                    active=active,
                    active_job_ids=active_job_ids,
                    workers=workers,
                    runner_id=runner_id,
                    config=config,
                    resource_capacity=resource_capacity,
                    active_jobs_snapshot=active_jobs_snapshot,
                    claimable=store.claimable_jobs(active_job_ids=active_job_ids),
                )
                if len(active) != before_launch:
                    store.renew_lease(
                        runner_id=runner_id,
                        now=utc_now(),
                        capacity=_capacity_usage(resource_capacity, _active_jobs(active)),
                    )
                    last_heartbeat = utc_now()

            if active:
                await asyncio.sleep(SCHEDULER_CONTROL_POLL_SECONDS)
                continue

            if current_mode in {SchedulerMode.DRAINING, SchedulerMode.STOPPING}:
                end_reason = "drained" if current_mode == SchedulerMode.DRAINING else "stopped"
                break

            if current_mode == SchedulerMode.PAUSED:
                await asyncio.sleep(SCHEDULER_CONTROL_POLL_SECONDS)
                continue

            if not store.has_pending_jobs():
                idle_since = idle_since or utc_now()
                if (utc_now() - idle_since).total_seconds() >= SCHEDULER_IDLE_EXIT_SECONDS:
                    break
            else:
                idle_since = None
            await asyncio.sleep(SCHEDULER_POLL_SECONDS)
    except BaseException:
        end_reason = "interrupted"
        if workers_paused:
            workers.resume_active_jobs()
            workers_paused = False
        await _cancel_active_workers_for_shutdown(store=store, active=active)
        raise
    finally:
        if workers_paused:
            workers.resume_active_jobs()
        if session_id is not None:
            store.end_session(session_id=session_id, now=utc_now(), reason=end_reason)
        store.release_lease(runner_id=runner_id, now=utc_now())

    terminal_counts = store.terminal_counts()
    return SchedulerRunSummary(
        completed=max(0, terminal_counts.completed - initial_terminal_counts.completed),
        failed=max(0, terminal_counts.failed - initial_terminal_counts.failed),
        skipped=max(0, terminal_counts.skipped - initial_terminal_counts.skipped),
        idle=True,
    )


def new_runner_id() -> str:
    return uuid.uuid4().hex


def cli_actor() -> str:
    return f"cli:{socket.gethostname()}:{os.getpid()}"


def _resource_capacity(config: AppConfig) -> ResourceCapacity:
    return ResourceCapacity(
        cheap_workers=config.resources.cheap_workers,
        av1an_jobs=config.resources.av1an_jobs,
        file_ops=config.resources.file_ops,
    )


def _active_jobs(active: dict[asyncio.Task[Any], ActiveJob]) -> list[ActiveJob]:
    return list(active.values())


def _capacity_usage(
    capacity: ResourceCapacity,
    active_jobs: list[ActiveJob],
) -> SchedulerCapacityUsage:
    cheap_active = 0
    av1an_active = 0
    file_ops_active = 0
    for job in active_jobs:
        resource_class = resource_for_stage(job.stage)
        if resource_class.value == "cheap":
            cheap_active += 1
        elif resource_class.value == "heavy_av1an":
            av1an_active += 1
        elif resource_class.value == "file_op":
            file_ops_active += 1
    return SchedulerCapacityUsage(
        cheap_workers=capacity.cheap_workers,
        cheap_active=cheap_active,
        av1an_jobs=capacity.av1an_jobs,
        av1an_active=av1an_active,
        file_ops=capacity.file_ops,
        file_ops_active=file_ops_active,
    )


def _cancel_active_tasks(
    active: dict[asyncio.Task[Any], ActiveJob],
    job_ids: set[int] | frozenset[int],
) -> None:
    for task, job in active.items():
        if job.job_id in job_ids:
            task.cancel()


async def _cancel_active_workers_for_shutdown(
    *,
    store: SchedulerRunStore,
    active: dict[asyncio.Task[Any], ActiveJob],
) -> None:
    if not active:
        return
    tasks = list(active)
    for task in tasks:
        task.cancel()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for task, result in zip(tasks, results, strict=True):
        job = active[task]
        if task.cancelled() or isinstance(result, BaseException):
            store.interrupt_running_job(job_id=job.job_id, now=utc_now())
        active.pop(task, None)


def _launch_claimable_jobs(
    *,
    active: dict[asyncio.Task[Any], ActiveJob],
    active_job_ids: set[int],
    workers: SchedulerWorkerRegistry,
    runner_id: str,
    config: AppConfig,
    resource_capacity: ResourceCapacity,
    active_jobs_snapshot: list[ActiveJob],
    claimable: list[ClaimableJob],
) -> None:
    launchable = select_launchable_jobs(
        claimable,
        active_jobs=active_jobs_snapshot,
        capacity=resource_capacity,
    )
    for job in launchable:
        task = asyncio.create_task(
            workers.run_job(
                stage=job.stage,
                job_id=job.job_id,
                runner_id=runner_id,
                config=config,
            )
        )
        active[task] = ActiveJob(job_id=job.job_id, stage=job.stage)
        active_job_ids.add(job.job_id)
