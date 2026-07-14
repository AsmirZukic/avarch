from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from avarch.application.scheduler_run import (
    SchedulerControlSnapshot,
    SchedulerTerminalCounts,
    run_scheduler,
)
from avarch.config import AppConfig
from avarch.domain.jobs import JobStage
from avarch.domain.scheduler import ClaimableJob, SchedulerMode


def test_run_scheduler_reraises_worker_cancellation_after_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("avarch.application.scheduler_run.SCHEDULER_CONTROL_POLL_SECONDS", 0)
    store = _Store()
    runtime = _Runtime(store)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(run_scheduler(runtime, config=AppConfig(), runner_id="runner"))

    assert store.interrupted_job_ids == [1]
    assert store.released is True


def test_run_scheduler_launches_distinct_resource_classes_concurrently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("avarch.application.scheduler_run.SCHEDULER_CONTROL_POLL_SECONDS", 0)
    monkeypatch.setattr("avarch.application.scheduler_run.SCHEDULER_POLL_SECONDS", 0)
    monkeypatch.setattr("avarch.application.scheduler_run.SCHEDULER_IDLE_EXIT_SECONDS", 0)
    store = _ConcurrentStore()
    workers = _RecordingWorkers()
    runtime = _ConcurrentRuntime(store, workers)

    summary = asyncio.run(run_scheduler(runtime, config=AppConfig(), runner_id="runner"))

    assert summary.idle is True
    assert workers.started == [JobStage.ENCODE, JobStage.VALIDATE, JobStage.PROMOTE]
    assert workers.peak_active == 3
    assert store.released is True


class _Store:
    def __init__(self) -> None:
        self._claimable_returned = False
        self.interrupted_job_ids: list[int] = []
        self.released = False

    def acquire_lease(self, *, runner_id: str, now: datetime, resume: bool) -> None:
        del runner_id, now, resume

    def recover_abandoned_jobs(self, *, now: datetime) -> None:
        del now

    def renew_lease(self, *, runner_id: str, now: datetime) -> None:
        del runner_id, now

    def load_control_snapshot(self, *, now: datetime) -> SchedulerControlSnapshot:
        del now
        return SchedulerControlSnapshot(
            mode=SchedulerMode.RUNNING,
            control_generation=0,
            acknowledged_generation=0,
            runner_id="runner",
        )

    def acknowledge_control(self, *, runner_id: str, now: datetime) -> None:
        del runner_id, now

    def cancel_requested_job_ids(self, *, job_ids: set[int]) -> set[int]:
        del job_ids
        return set()

    def claimable_jobs(self, *, active_job_ids: set[int]) -> list[ClaimableJob]:
        del active_job_ids
        if self._claimable_returned:
            return []
        self._claimable_returned = True
        return [ClaimableJob(job_id=1, stage=JobStage.PROBE)]

    def has_pending_jobs(self) -> bool:
        return False

    def interrupt_running_job(self, *, job_id: int, now: datetime) -> None:
        del now
        self.interrupted_job_ids.append(job_id)

    def release_lease(self, *, runner_id: str, now: datetime) -> None:
        del runner_id, now
        self.released = True

    def terminal_counts(self) -> SchedulerTerminalCounts:
        return SchedulerTerminalCounts(completed=0, failed=0, skipped=0)


class _Workers:
    async def run_job(
        self,
        *,
        stage: JobStage,
        job_id: int,
        runner_id: str,
        config: AppConfig,
    ) -> None:
        del stage, job_id, runner_id, config
        raise asyncio.CancelledError


class _Runtime:
    def __init__(self, store: _Store) -> None:
        self._store = store

    def store(self, *, config: AppConfig) -> _Store:
        del config
        return self._store

    def workers(self) -> _Workers:
        return _Workers()


class _ConcurrentStore:
    def __init__(self) -> None:
        self._claimable_returned = False
        self.released = False

    def acquire_lease(self, *, runner_id: str, now: datetime, resume: bool) -> None:
        del runner_id, now, resume

    def recover_abandoned_jobs(self, *, now: datetime) -> None:
        del now

    def renew_lease(self, *, runner_id: str, now: datetime) -> None:
        del runner_id, now

    def load_control_snapshot(self, *, now: datetime) -> SchedulerControlSnapshot:
        del now
        return SchedulerControlSnapshot(
            mode=SchedulerMode.RUNNING,
            control_generation=0,
            acknowledged_generation=0,
            runner_id="runner",
        )

    def acknowledge_control(self, *, runner_id: str, now: datetime) -> None:
        del runner_id, now

    def cancel_requested_job_ids(self, *, job_ids: set[int]) -> set[int]:
        del job_ids
        return set()

    def claimable_jobs(self, *, active_job_ids: set[int]) -> list[ClaimableJob]:
        del active_job_ids
        if self._claimable_returned:
            return []
        self._claimable_returned = True
        return [
            ClaimableJob(job_id=1, stage=JobStage.ENCODE),
            ClaimableJob(job_id=2, stage=JobStage.VALIDATE),
            ClaimableJob(job_id=3, stage=JobStage.PROMOTE),
        ]

    def has_pending_jobs(self) -> bool:
        return False

    def interrupt_running_job(self, *, job_id: int, now: datetime) -> None:
        del job_id, now

    def release_lease(self, *, runner_id: str, now: datetime) -> None:
        del runner_id, now
        self.released = True

    def terminal_counts(self) -> SchedulerTerminalCounts:
        return SchedulerTerminalCounts(completed=0, failed=0, skipped=0)


class _RecordingWorkers:
    def __init__(self) -> None:
        self.started: list[JobStage] = []
        self._active = 0
        self.peak_active = 0

    async def run_job(
        self,
        *,
        stage: JobStage,
        job_id: int,
        runner_id: str,
        config: AppConfig,
    ) -> None:
        del job_id, runner_id, config
        self.started.append(stage)
        self._active += 1
        self.peak_active = max(self.peak_active, self._active)
        await asyncio.sleep(0)
        self._active -= 1


class _ConcurrentRuntime:
    def __init__(self, store: _ConcurrentStore, workers: _RecordingWorkers) -> None:
        self._store = store
        self._workers = workers

    def store(self, *, config: AppConfig) -> _ConcurrentStore:
        del config
        return self._store

    def workers(self) -> _RecordingWorkers:
        return self._workers
