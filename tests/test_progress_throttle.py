from __future__ import annotations

from datetime import UTC, datetime, timedelta

from avarch.application.progress import (
    DEFAULT_PROGRESS_PERSISTENCE_INTERVAL_SECONDS,
    ProgressPersistenceThrottle,
    RecordingProgressSink,
)
from avarch.domain.progress import ProgressPhase, ProgressSnapshot, ProgressSource, ProgressUnit


def test_throttle_persists_first_snapshot_immediately() -> None:
    clock = _FakeClock()
    sink = RecordingProgressSink()
    throttle = ProgressPersistenceThrottle(sink, clock=clock.monotonic)

    throttle.publish(_snapshot(current=1))

    assert [snapshot.current for snapshot in sink.snapshots] == [1]


def test_throttle_persists_phase_transition_immediately() -> None:
    clock = _FakeClock()
    sink = RecordingProgressSink()
    throttle = ProgressPersistenceThrottle(sink, clock=clock.monotonic)

    throttle.publish(_snapshot(phase=ProgressPhase.PREPARING, current=None, unit=None))
    throttle.publish(_snapshot(current=1))

    assert [snapshot.phase for snapshot in sink.snapshots] == [
        ProgressPhase.PREPARING,
        ProgressPhase.ENCODING,
    ]


def test_throttle_persists_terminal_snapshot_immediately() -> None:
    clock = _FakeClock()
    sink = RecordingProgressSink()
    throttle = ProgressPersistenceThrottle(sink, clock=clock.monotonic)

    throttle.publish(_snapshot(current=1))
    throttle.publish(_snapshot(current=2))
    throttle.publish(_snapshot(phase=ProgressPhase.COMPLETED, current=100))

    assert [snapshot.phase for snapshot in sink.snapshots] == [
        ProgressPhase.ENCODING,
        ProgressPhase.COMPLETED,
    ]


def test_throttle_coalesces_ordinary_updates_inside_interval() -> None:
    clock = _FakeClock()
    sink = RecordingProgressSink()
    throttle = ProgressPersistenceThrottle(sink, clock=clock.monotonic)

    throttle.publish(_snapshot(current=1))
    for value in range(2, 20):
        throttle.publish(_snapshot(current=value))

    assert [snapshot.current for snapshot in sink.snapshots] == [1]


def test_throttle_flushes_latest_ordinary_update_after_interval() -> None:
    clock = _FakeClock()
    sink = RecordingProgressSink()
    throttle = ProgressPersistenceThrottle(sink, clock=clock.monotonic)

    throttle.publish(_snapshot(current=1))
    throttle.publish(_snapshot(current=2))
    throttle.publish(_snapshot(current=3))
    clock.advance(DEFAULT_PROGRESS_PERSISTENCE_INTERVAL_SECONDS)

    assert throttle.flush_due() is True
    assert [snapshot.current for snapshot in sink.snapshots] == [1, 3]


def test_throttle_bounds_write_count_under_heavy_samples() -> None:
    clock = _FakeClock()
    sink = RecordingProgressSink()
    throttle = ProgressPersistenceThrottle(sink, clock=clock.monotonic)

    throttle.publish(_snapshot(current=0))
    for value in range(1, 10_000):
        throttle.publish(_snapshot(current=value))

    assert len(sink.snapshots) == 1
    clock.advance(DEFAULT_PROGRESS_PERSISTENCE_INTERVAL_SECONDS)
    throttle.flush_due()
    assert [snapshot.current for snapshot in sink.snapshots] == [0, 9_999]


def test_throttle_shutdown_flushes_pending_snapshot() -> None:
    clock = _FakeClock()
    sink = RecordingProgressSink()
    throttle = ProgressPersistenceThrottle(sink, clock=clock.monotonic)

    throttle.publish(_snapshot(current=1))
    throttle.publish(_snapshot(current=2))

    assert throttle.close() is True
    assert [snapshot.current for snapshot in sink.snapshots] == [1, 2]


class _FakeClock:
    def __init__(self) -> None:
        self._value = 0.0

    def monotonic(self) -> float:
        return self._value

    def advance(self, seconds: float) -> None:
        self._value += seconds


def _snapshot(
    *,
    phase: ProgressPhase = ProgressPhase.ENCODING,
    current: int | None = 1,
    unit: ProgressUnit | None = ProgressUnit.FRAMES,
) -> ProgressSnapshot:
    now = datetime(2026, 7, 1, 12, tzinfo=UTC)
    if current is not None:
        now += timedelta(seconds=current)
    return ProgressSnapshot(
        phase=phase,
        current=current,
        total=10_000 if current is not None else None,
        unit=unit,
        rate_per_second=None,
        speed_ratio=None,
        source=ProgressSource.SCHEDULER,
        message=None,
        phase_started_at=datetime(2026, 7, 1, 12, tzinfo=UTC),
        observed_at=now,
        heartbeat_at=now,
        advanced_at=now if current is not None else None,
    )
