from __future__ import annotations

from collections.abc import Callable

from avarch.domain.jobs import JobStatus

MonotonicClock = Callable[[], float]
Sleep = Callable[[float], None]


def wait_for_scheduler_inactive(
    *,
    load_lease_state: Callable[[], str],
    timeout_seconds: float,
    monotonic: MonotonicClock,
    sleep: Sleep,
    poll_interval_seconds: float = 0.25,
) -> bool:
    return _wait_until(
        lambda: load_lease_state() == "inactive",
        timeout_seconds=timeout_seconds,
        monotonic=monotonic,
        sleep=sleep,
        poll_interval_seconds=poll_interval_seconds,
    )


def wait_for_job_status(
    *,
    load_job_status: Callable[[], JobStatus | None],
    expected: JobStatus,
    timeout_seconds: float,
    monotonic: MonotonicClock,
    sleep: Sleep,
    poll_interval_seconds: float = 0.25,
) -> bool:
    return _wait_until(
        lambda: load_job_status() == expected,
        timeout_seconds=timeout_seconds,
        monotonic=monotonic,
        sleep=sleep,
        poll_interval_seconds=poll_interval_seconds,
    )


def wait_for_queue_clear(
    *,
    has_running_cancel_requests: Callable[[], bool],
    timeout_seconds: float,
    monotonic: MonotonicClock,
    sleep: Sleep,
    poll_interval_seconds: float = 0.25,
) -> bool:
    return _wait_until(
        lambda: not has_running_cancel_requests(),
        timeout_seconds=timeout_seconds,
        monotonic=monotonic,
        sleep=sleep,
        poll_interval_seconds=poll_interval_seconds,
    )


def _wait_until(
    predicate: Callable[[], bool],
    *,
    timeout_seconds: float,
    monotonic: MonotonicClock,
    sleep: Sleep,
    poll_interval_seconds: float,
) -> bool:
    deadline = monotonic() + timeout_seconds
    while monotonic() <= deadline:
        if predicate():
            return True
        sleep(poll_interval_seconds)
    return False
