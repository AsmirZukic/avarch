from __future__ import annotations

import asyncio
from datetime import datetime

import pytest

from avarch.application.scheduler_run import (
    SchedulerCapacityUsage,
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
    assert store.session_end_reasons == ["interrupted"]
    assert store.released is True


def test_run_scheduler_cancels_active_worker_on_outer_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("avarch.application.scheduler_run.SCHEDULER_CONTROL_POLL_SECONDS", 10)
    store = _Store()

    async def scenario() -> _SlowWorkers:
        workers = _SlowWorkers()
        runtime = _SlowRuntime(store, workers)
        task = asyncio.create_task(
            run_scheduler(runtime, config=AppConfig(), runner_id="runner")
        )
        await asyncio.wait_for(workers.started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return workers

    workers = asyncio.run(scenario())

    assert workers.cancelled is True
    assert store.interrupted_job_ids == [1]
    assert store.session_end_reasons == ["interrupted"]
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
    assert store.capacity_usages[0].av1an_active == 0
    assert any(usage.av1an_active == 1 for usage in store.capacity_usages)
    assert any(usage.cheap_active == 1 for usage in store.capacity_usages)
    assert any(usage.file_ops_active == 1 for usage in store.capacity_usages)
    assert store.capacity_usages[-1].av1an_active == 0
    assert store.capacity_usages[-1].cheap_active == 0
    assert store.capacity_usages[-1].file_ops_active == 0
    assert store.session_end_reasons == ["normal"]
    assert store.released is True


def test_run_scheduler_reports_terminal_count_deltas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("avarch.application.scheduler_run.SCHEDULER_POLL_SECONDS", 0)
    monkeypatch.setattr("avarch.application.scheduler_run.SCHEDULER_IDLE_EXIT_SECONDS", 0)
    store = _HistoricalTerminalCountsStore()
    runtime = _HistoricalTerminalCountsRuntime(store)

    summary = asyncio.run(run_scheduler(runtime, config=AppConfig(), runner_id="runner"))

    assert summary.completed == 0
    assert summary.failed == 0
    assert summary.skipped == 0
    assert store.session_end_reasons == ["normal"]
    assert store.released is True


def test_run_scheduler_starts_session_after_acquiring_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("avarch.application.scheduler_run.SCHEDULER_POLL_SECONDS", 0)
    monkeypatch.setattr("avarch.application.scheduler_run.SCHEDULER_IDLE_EXIT_SECONDS", 0)
    store = _OrderingStore()
    runtime = _OrderingRuntime(store)

    asyncio.run(run_scheduler(runtime, config=AppConfig(), runner_id="runner"))

    assert store.calls[:2] == ["acquire_lease", "start_session"]
    assert store.calls[-2:] == ["end_session:normal", "release_lease"]


def test_run_scheduler_records_drain_completion_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("avarch.application.scheduler_run.SCHEDULER_CONTROL_POLL_SECONDS", 0)
    store = _DrainStore()
    runtime = _DrainRuntime(store)

    asyncio.run(run_scheduler(runtime, config=AppConfig(), runner_id="runner"))

    assert store.session_end_reasons == ["drained"]
    assert store.released is True


def test_run_scheduler_pauses_and_resumes_active_managed_jobs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("avarch.application.scheduler_run.SCHEDULER_CONTROL_POLL_SECONDS", 0)
    store = _PauseStore()
    workers = _PauseWorkers()

    asyncio.run(
        run_scheduler(
            _PauseRuntime(store, workers),
            config=AppConfig(),
            runner_id="runner",
        )
    )

    assert workers.control_calls == ["pause", "resume"]
    assert store.session_end_reasons == ["stopped"]


def test_run_scheduler_releases_lease_when_session_start_fails() -> None:
    store = _FailingSessionStartStore()
    runtime = _FailingSessionStartRuntime(store)

    with pytest.raises(RuntimeError, match="session failed"):
        asyncio.run(run_scheduler(runtime, config=AppConfig(), runner_id="runner"))

    assert store.released is True
    assert store.session_end_reasons == []


class _Store:
    def __init__(self) -> None:
        self._claimable_returned = False
        self.interrupted_job_ids: list[int] = []
        self.session_end_reasons: list[str] = []
        self.released = False

    def acquire_lease(
        self,
        *,
        runner_id: str,
        now: datetime,
        resume: bool,
        capacity: SchedulerCapacityUsage,
    ) -> None:
        del runner_id, now, resume, capacity

    def start_session(self, *, runner_id: str, now: datetime) -> int:
        del runner_id, now
        return 1

    def recover_abandoned_jobs(self, *, now: datetime) -> None:
        del now

    def renew_lease(
        self,
        *,
        runner_id: str,
        now: datetime,
        capacity: SchedulerCapacityUsage,
    ) -> None:
        del runner_id, now, capacity

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

    def end_session(self, *, session_id: int, now: datetime, reason: str) -> None:
        del session_id, now
        self.session_end_reasons.append(reason)

    def release_lease(self, *, runner_id: str, now: datetime) -> None:
        del runner_id, now
        self.released = True

    def terminal_counts(self) -> SchedulerTerminalCounts:
        return SchedulerTerminalCounts(completed=0, failed=0, skipped=0)


class _PauseStore(_Store):
    def __init__(self) -> None:
        super().__init__()
        self._modes = [
            SchedulerMode.RUNNING,
            SchedulerMode.PAUSED,
            SchedulerMode.RUNNING,
            SchedulerMode.STOPPING,
        ]

    def load_control_snapshot(self, *, now: datetime) -> SchedulerControlSnapshot:
        del now
        mode = self._modes.pop(0) if self._modes else SchedulerMode.STOPPING
        return SchedulerControlSnapshot(
            mode=mode,
            control_generation=0,
            acknowledged_generation=0,
            runner_id="runner",
        )


class _PauseWorkers:
    def __init__(self) -> None:
        self.control_calls: list[str] = []

    async def run_job(
        self,
        *,
        stage: JobStage,
        job_id: int,
        runner_id: str,
        config: AppConfig,
    ) -> None:
        del stage, job_id, runner_id, config
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            return

    def pause_active_jobs(self) -> None:
        self.control_calls.append("pause")

    def resume_active_jobs(self) -> None:
        self.control_calls.append("resume")


class _PauseRuntime:
    def __init__(self, store: _PauseStore, workers: _PauseWorkers) -> None:
        self._store = store
        self._workers = workers

    def store(self, *, config: AppConfig) -> _PauseStore:
        del config
        return self._store

    def workers(self) -> _PauseWorkers:
        return self._workers


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


class _SlowWorkers:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = False

    async def run_job(
        self,
        *,
        stage: JobStage,
        job_id: int,
        runner_id: str,
        config: AppConfig,
    ) -> None:
        del stage, job_id, runner_id, config
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class _SlowRuntime:
    def __init__(self, store: _Store, workers: _SlowWorkers) -> None:
        self._store = store
        self._workers = workers

    def store(self, *, config: AppConfig) -> _Store:
        del config
        return self._store

    def workers(self) -> _SlowWorkers:
        return self._workers


class _ConcurrentStore:
    def __init__(self) -> None:
        self._claimable_returned = False
        self.released = False
        self.session_end_reasons: list[str] = []
        self.capacity_usages: list[SchedulerCapacityUsage] = []

    def acquire_lease(
        self,
        *,
        runner_id: str,
        now: datetime,
        resume: bool,
        capacity: SchedulerCapacityUsage,
    ) -> None:
        del runner_id, now, resume
        self.capacity_usages.append(capacity)

    def start_session(self, *, runner_id: str, now: datetime) -> int:
        del runner_id, now
        return 1

    def recover_abandoned_jobs(self, *, now: datetime) -> None:
        del now

    def renew_lease(
        self,
        *,
        runner_id: str,
        now: datetime,
        capacity: SchedulerCapacityUsage,
    ) -> None:
        del runner_id, now
        self.capacity_usages.append(capacity)

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

    def end_session(self, *, session_id: int, now: datetime, reason: str) -> None:
        del session_id, now
        self.session_end_reasons.append(reason)

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


class _HistoricalTerminalCountsStore:
    def __init__(self) -> None:
        self.released = False
        self.session_end_reasons: list[str] = []

    def acquire_lease(
        self,
        *,
        runner_id: str,
        now: datetime,
        resume: bool,
        capacity: SchedulerCapacityUsage,
    ) -> None:
        del runner_id, now, resume, capacity

    def start_session(self, *, runner_id: str, now: datetime) -> int:
        del runner_id, now
        return 1

    def recover_abandoned_jobs(self, *, now: datetime) -> None:
        del now

    def renew_lease(
        self,
        *,
        runner_id: str,
        now: datetime,
        capacity: SchedulerCapacityUsage,
    ) -> None:
        del runner_id, now, capacity

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
        return []

    def has_pending_jobs(self) -> bool:
        return False

    def interrupt_running_job(self, *, job_id: int, now: datetime) -> None:
        del job_id, now

    def end_session(self, *, session_id: int, now: datetime, reason: str) -> None:
        del session_id, now
        self.session_end_reasons.append(reason)

    def release_lease(self, *, runner_id: str, now: datetime) -> None:
        del runner_id, now
        self.released = True

    def terminal_counts(self) -> SchedulerTerminalCounts:
        return SchedulerTerminalCounts(completed=2, failed=1, skipped=3)


class _HistoricalTerminalCountsRuntime:
    def __init__(self, store: _HistoricalTerminalCountsStore) -> None:
        self._store = store

    def store(self, *, config: AppConfig) -> _HistoricalTerminalCountsStore:
        del config
        return self._store

    def workers(self) -> _RecordingWorkers:
        return _RecordingWorkers()


class _OrderingStore(_HistoricalTerminalCountsStore):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []

    def acquire_lease(
        self,
        *,
        runner_id: str,
        now: datetime,
        resume: bool,
        capacity: SchedulerCapacityUsage,
    ) -> None:
        del runner_id, now, resume, capacity
        self.calls.append("acquire_lease")

    def start_session(self, *, runner_id: str, now: datetime) -> int:
        del runner_id, now
        self.calls.append("start_session")
        return 1

    def end_session(self, *, session_id: int, now: datetime, reason: str) -> None:
        del session_id, now
        self.calls.append(f"end_session:{reason}")
        self.session_end_reasons.append(reason)

    def release_lease(self, *, runner_id: str, now: datetime) -> None:
        del runner_id, now
        self.calls.append("release_lease")
        self.released = True


class _OrderingRuntime(_HistoricalTerminalCountsRuntime):
    def __init__(self, store: _OrderingStore) -> None:
        self._store = store

    def store(self, *, config: AppConfig) -> _OrderingStore:
        del config
        return self._store


class _DrainStore(_HistoricalTerminalCountsStore):
    def load_control_snapshot(self, *, now: datetime) -> SchedulerControlSnapshot:
        del now
        return SchedulerControlSnapshot(
            mode=SchedulerMode.DRAINING,
            control_generation=1,
            acknowledged_generation=1,
            runner_id="runner",
        )


class _DrainRuntime(_HistoricalTerminalCountsRuntime):
    def __init__(self, store: _DrainStore) -> None:
        self._store = store

    def store(self, *, config: AppConfig) -> _DrainStore:
        del config
        return self._store


class _FailingSessionStartStore(_HistoricalTerminalCountsStore):
    def start_session(self, *, runner_id: str, now: datetime) -> int:
        del runner_id, now
        raise RuntimeError("session failed")


class _FailingSessionStartRuntime(_HistoricalTerminalCountsRuntime):
    def __init__(self, store: _FailingSessionStartStore) -> None:
        self._store = store

    def store(self, *, config: AppConfig) -> _FailingSessionStartStore:
        del config
        return self._store
