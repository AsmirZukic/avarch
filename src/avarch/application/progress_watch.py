from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from avarch.application.job_views import CurrentJobProgressView
from avarch.application.progress_views import JobProgressView, job_progress_view
from avarch.domain.jobs import JobStatus

DEFAULT_PROGRESS_WATCH_POLL_INTERVAL = 1.0
PROGRESS_WATCH_TERMINAL_JOB_STATUSES = frozenset(
    {
        JobStatus.PROMOTED,
        JobStatus.SKIPPED,
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.VALIDATION_FAILED,
        JobStatus.SIZE_REJECTED,
        JobStatus.READY_TO_PROMOTE,
    }
)


class JobProgressReader(Protocol):
    def __call__(self, job_id: int, /) -> CurrentJobProgressView | None: ...


class JobProgressWatchError(RuntimeError):
    pass


class JobProgressWatchNotFound(JobProgressWatchError):
    pass


class JobProgressWatchReadError(JobProgressWatchError):
    pass


class JobProgressWatchInterrupted(JobProgressWatchError):
    pass


@dataclass(frozen=True, slots=True)
class JobProgressWatchUpdate:
    view: JobProgressView
    terminal: bool


def watch_job_progress(
    *,
    read_current: JobProgressReader,
    job_id: int,
    on_update: Callable[[JobProgressWatchUpdate], None],
    now: Callable[[], datetime],
    sleep: Callable[[float], None],
    poll_interval: float = DEFAULT_PROGRESS_WATCH_POLL_INTERVAL,
) -> JobProgressView:
    if poll_interval <= 0:
        raise ValueError("poll_interval must be positive")

    previous_fingerprint: tuple[object, ...] | None = None
    while True:
        view = _read_progress(read_current, job_id=job_id, now=now())
        terminal = job_progress_watch_is_terminal(view)
        fingerprint = _watch_fingerprint(view)
        if fingerprint != previous_fingerprint:
            on_update(JobProgressWatchUpdate(view=view, terminal=terminal))
            previous_fingerprint = fingerprint
        if terminal:
            return view
        try:
            sleep(poll_interval)
        except KeyboardInterrupt as exc:
            raise JobProgressWatchInterrupted("Progress watch interrupted") from exc


def job_progress_watch_is_terminal(view: JobProgressView) -> bool:
    return view.job_status in PROGRESS_WATCH_TERMINAL_JOB_STATUSES


def _read_progress(
    read_current: JobProgressReader,
    *,
    job_id: int,
    now: datetime,
) -> JobProgressView:
    try:
        current = read_current(job_id)
    except Exception as exc:
        raise JobProgressWatchReadError("Unable to read job progress") from exc
    view = job_progress_view(current, now=now)
    if view is None:
        raise JobProgressWatchNotFound(f"Job not found: {job_id}")
    return view


def _watch_fingerprint(view: JobProgressView) -> tuple[object, ...]:
    return (
        view.job_status,
        view.job_stage,
        view.attempt_id,
        view.attempt_number,
        view.attempt_status,
        view.phase,
        view.current,
        view.total,
        view.unit,
        view.percent,
        _duration_seconds(view.eta),
        view.rate_per_second,
        view.speed_ratio,
        view.source,
        view.heartbeat_stale,
        view.not_advancing,
        view.message,
        view.observed_at,
        view.heartbeat_at,
        view.advanced_at,
    )


def _duration_seconds(duration: timedelta | None) -> int | None:
    if duration is None:
        return None
    return int(duration.total_seconds())
