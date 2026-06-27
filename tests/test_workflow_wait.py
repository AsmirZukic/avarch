from __future__ import annotations

from avarch.application.workflow_wait import (
    wait_for_job_status,
    wait_for_queue_clear,
    wait_for_scheduler_inactive,
)
from avarch.domain.jobs import JobStatus


def test_wait_for_scheduler_inactive_polls_until_inactive() -> None:
    clock = _FakeClock()
    states = iter(["running", "running", "inactive"])

    result = wait_for_scheduler_inactive(
        load_lease_state=lambda: next(states),
        timeout_seconds=5,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert result is True
    assert clock.sleeps == [0.25, 0.25]


def test_wait_for_job_status_times_out_when_status_never_matches() -> None:
    clock = _FakeClock()

    result = wait_for_job_status(
        load_job_status=lambda: JobStatus.ENCODING,
        expected=JobStatus.CANCELLED,
        timeout_seconds=0.5,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert result is False
    assert clock.sleeps == [0.25, 0.25, 0.25]


def test_wait_for_queue_clear_returns_when_running_cancels_are_gone() -> None:
    clock = _FakeClock()
    pending = iter([True, False])

    result = wait_for_queue_clear(
        has_running_cancel_requests=lambda: next(pending),
        timeout_seconds=5,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert result is True
    assert clock.sleeps == [0.25]


class _FakeClock:
    def __init__(self) -> None:
        self._now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self._now += seconds
