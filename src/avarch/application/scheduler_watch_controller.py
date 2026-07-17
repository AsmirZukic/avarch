from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from avarch.application.job_control import JobControlStore, cancel_jobs
from avarch.application.job_views import JobDetails, JobViewStore, job_details, latest_attempt
from avarch.application.scheduler_control import (
    SchedulerControlStore,
    pause_scheduler,
    resume_scheduler,
    stop_scheduler,
)


@dataclass(frozen=True, slots=True)
class WatchControlResult:
    action: str
    message: str


@dataclass(frozen=True, slots=True)
class WatchLogPaths:
    stdout_log: str | None
    stderr_log: str | None


class WatchController(Protocol):
    def pause(self, *, reason: str | None = None) -> WatchControlResult: ...

    def resume(self) -> WatchControlResult: ...

    def cancel(self, *, job_id: int, reason: str | None = None) -> WatchControlResult: ...

    def details(self, *, job_id: int) -> JobDetails | None: ...

    def log_paths(self, *, job_id: int, attempt_number: int | None = None) -> WatchLogPaths: ...

    def detach(self) -> WatchControlResult: ...

    def stop(self, *, reason: str | None = None) -> WatchControlResult: ...


class SchedulerWatchController:
    def __init__(
        self,
        *,
        scheduler_store: SchedulerControlStore,
        job_store: JobControlStore,
        job_view_store: JobViewStore,
        actor: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._scheduler_store = scheduler_store
        self._job_store = job_store
        self._job_view_store = job_view_store
        self._actor = actor
        self._clock = clock or (lambda: datetime.now(UTC))

    def pause(self, *, reason: str | None = None) -> WatchControlResult:
        pause_scheduler(self._scheduler_store, now=self._clock(), reason=reason)
        return WatchControlResult(action="pause", message="Scheduler pause requested.")

    def resume(self) -> WatchControlResult:
        resume_scheduler(self._scheduler_store, now=self._clock())
        return WatchControlResult(action="resume", message="Scheduler resume requested.")

    def cancel(self, *, job_id: int, reason: str | None = None) -> WatchControlResult:
        cancel_jobs(
            self._job_store,
            job_id=job_id,
            running=False,
            actor=self._actor,
            now=self._clock(),
            reason=reason,
        )
        return WatchControlResult(action="cancel", message=f"Job {job_id} cancellation requested.")

    def details(self, *, job_id: int) -> JobDetails | None:
        return job_details(self._job_view_store, job_id=job_id)

    def log_paths(self, *, job_id: int, attempt_number: int | None = None) -> WatchLogPaths:
        attempt = latest_attempt(
            self._job_view_store,
            job_id=job_id,
            attempt_number=attempt_number,
        )
        if attempt is None:
            return WatchLogPaths(stdout_log=None, stderr_log=None)
        return WatchLogPaths(stdout_log=attempt.stdout_log, stderr_log=attempt.stderr_log)

    def detach(self) -> WatchControlResult:
        return WatchControlResult(action="detach", message="Detached from scheduler watch.")

    def stop(self, *, reason: str | None = None) -> WatchControlResult:
        stop_scheduler(self._scheduler_store, now=self._clock(), reason=reason)
        return WatchControlResult(action="stop", message="Scheduler stop requested.")
