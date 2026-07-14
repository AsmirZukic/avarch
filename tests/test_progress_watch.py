from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

import pytest

from avarch.application.job_views import CurrentJobProgressView, JobAttemptView
from avarch.application.progress_views import JobProgressView, job_progress_view
from avarch.application.progress_watch import (
    JobProgressReader,
    JobProgressWatchInterrupted,
    JobProgressWatchNotFound,
    JobProgressWatchReadError,
    JobProgressWatchUpdate,
    job_progress_watch_is_terminal,
    watch_job_progress,
)
from avarch.domain.jobs import AttemptStatus, JobStage, JobStatus
from avarch.domain.progress import ProgressPhase, ProgressSnapshot, ProgressSource, ProgressUnit

NOW = datetime(2026, 7, 1, 12, tzinfo=UTC)


def test_watch_publishes_initial_read_and_exits_on_terminal() -> None:
    updates: list[JobProgressWatchUpdate] = []

    final = watch_job_progress(
        read_current=_reader([_current(status=JobStatus.PROMOTED, phase=ProgressPhase.COMPLETED)]),
        job_id=1,
        on_update=updates.append,
        now=lambda: NOW,
        sleep=_unused_sleep,
    )

    assert final.job_status == JobStatus.PROMOTED
    assert [update.view.phase for update in updates] == [ProgressPhase.COMPLETED]
    assert updates[0].terminal is True


def test_watch_polls_periodically_until_terminal() -> None:
    sleeps: list[float] = []
    updates: list[JobProgressWatchUpdate] = []

    watch_job_progress(
        read_current=_reader(
            [
                _current(current=1),
                _current(status=JobStatus.PROMOTED, phase=ProgressPhase.COMPLETED, current=2),
            ]
        ),
        job_id=1,
        on_update=updates.append,
        now=lambda: NOW,
        sleep=sleeps.append,
        poll_interval=0.25,
    )

    assert sleeps == [0.25]
    assert [update.view.current for update in updates] == [1, 2]


def test_watch_suppresses_unchanged_updates() -> None:
    updates: list[JobProgressWatchUpdate] = []
    sleeps = _interrupting_sleep(after=2)

    with pytest.raises(JobProgressWatchInterrupted):
        watch_job_progress(
            read_current=_reader(
                [_current(current=10), _current(current=10), _current(current=10)]
            ),
            job_id=1,
            on_update=updates.append,
            now=lambda: NOW,
            sleep=sleeps,
        )

    assert [update.view.current for update in updates] == [10]


def test_watch_reports_job_not_found() -> None:
    with pytest.raises(JobProgressWatchNotFound):
        watch_job_progress(
            read_current=lambda _job_id: None,
            job_id=999,
            on_update=lambda _update: None,
            now=lambda: NOW,
            sleep=_unused_sleep,
        )


def test_watch_publishes_attempt_replacement_after_retry() -> None:
    updates: list[JobProgressWatchUpdate] = []

    watch_job_progress(
        read_current=_reader(
            [
                _current(attempt_id=1, attempt_number=1, current=90),
                _current(attempt_id=2, attempt_number=2, current=5),
                _current(status=JobStatus.PROMOTED, phase=ProgressPhase.COMPLETED, attempt_id=2),
            ]
        ),
        job_id=1,
        on_update=updates.append,
        now=lambda: NOW,
        sleep=lambda _seconds: None,
    )

    assert [(update.view.attempt_id, update.view.current) for update in updates] == [
        (1, 90),
        (2, 5),
        (2, 10),
    ]


def test_watch_wraps_database_read_failure() -> None:
    def fail(_job_id: int) -> CurrentJobProgressView | None:
        raise RuntimeError("database locked")

    with pytest.raises(JobProgressWatchReadError):
        watch_job_progress(
            read_current=fail,
            job_id=1,
            on_update=lambda _update: None,
            now=lambda: NOW,
            sleep=_unused_sleep,
        )


def test_watch_converts_keyboard_interrupt() -> None:
    updates: list[JobProgressWatchUpdate] = []

    with pytest.raises(JobProgressWatchInterrupted):
        watch_job_progress(
            read_current=_reader([_current()]),
            job_id=1,
            on_update=updates.append,
            now=lambda: NOW,
            sleep=lambda _seconds: (_ for _ in ()).throw(KeyboardInterrupt),
        )

    assert len(updates) == 1


def test_watch_publishes_when_heartbeat_becomes_stale() -> None:
    times = iter([NOW, NOW + timedelta(seconds=45), NOW + timedelta(seconds=46)])
    updates: list[JobProgressWatchUpdate] = []

    with pytest.raises(JobProgressWatchInterrupted):
        watch_job_progress(
            read_current=_reader([_current(heartbeat_at=NOW), _current(heartbeat_at=NOW)]),
            job_id=1,
            on_update=updates.append,
            now=lambda: next(times),
            sleep=_interrupting_sleep(after=2),
        )

    assert [update.view.heartbeat_stale for update in updates] == [False, True]


def test_terminal_status_detection() -> None:
    assert job_progress_watch_is_terminal(_view(JobStatus.PROMOTED)) is True
    assert job_progress_watch_is_terminal(_view(JobStatus.READY_TO_PROMOTE)) is True
    assert job_progress_watch_is_terminal(_view(JobStatus.ENCODING)) is False


def _reader(
    values: Sequence[CurrentJobProgressView],
) -> JobProgressReader:
    items = iter(values)

    def read(_job_id: int) -> CurrentJobProgressView | None:
        return next(items)

    return read


def _unused_sleep(_seconds: float) -> None:
    raise AssertionError("sleep should not be called")


def _interrupting_sleep(*, after: int) -> Callable[[float], None]:
    calls = {"count": 0}

    def sleep(_seconds: float) -> None:
        calls["count"] += 1
        if calls["count"] >= after:
            raise KeyboardInterrupt

    return sleep


def _view(status: JobStatus) -> JobProgressView:
    view = job_progress_view(_current(status=status), now=NOW)
    assert view is not None
    return view


def _current(
    *,
    status: JobStatus = JobStatus.ENCODING,
    phase: ProgressPhase = ProgressPhase.ENCODING,
    attempt_id: int = 42,
    attempt_number: int = 1,
    current: float = 10,
    heartbeat_at: datetime | None = None,
) -> CurrentJobProgressView:
    return CurrentJobProgressView(
        job_id=1,
        job_status=status,
        job_stage=JobStage.ENCODE,
        attempt_id=attempt_id,
        attempt=JobAttemptView(
            attempt_number=attempt_number,
            stage=JobStage.ENCODE,
            status=AttemptStatus.COMPLETED
            if status != JobStatus.ENCODING
            else AttemptStatus.RUNNING,
            runner_id="runner",
            stdout_log=None,
            stderr_log=None,
        ),
        progress=ProgressSnapshot(
            phase=phase,
            current=current,
            total=100,
            unit=ProgressUnit.FRAMES,
            rate_per_second=10,
            speed_ratio=None,
            source=ProgressSource.SCHEDULER,
            message=None,
            phase_started_at=NOW - timedelta(seconds=10),
            observed_at=NOW,
            heartbeat_at=heartbeat_at or NOW,
            advanced_at=NOW,
        ),
    )
