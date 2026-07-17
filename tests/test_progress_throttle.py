from __future__ import annotations

import threading
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


def test_throttle_merges_heartbeat_into_pending_numeric_snapshot() -> None:
    clock = _FakeClock()
    sink = RecordingProgressSink()
    throttle = ProgressPersistenceThrottle(sink, clock=clock.monotonic)

    throttle.publish(_snapshot(current=1))
    throttle.publish(_snapshot(current=25))
    throttle.publish(_snapshot(current=None, unit=None, observed_offset_seconds=30))
    clock.advance(DEFAULT_PROGRESS_PERSISTENCE_INTERVAL_SECONDS)

    assert throttle.flush_due() is True
    assert [snapshot.current for snapshot in sink.snapshots] == [1, 25]
    assert sink.snapshots[-1].advanced_at == datetime(2026, 7, 1, 12, 0, 25, tzinfo=UTC)
    assert sink.snapshots[-1].heartbeat_at == datetime(2026, 7, 1, 12, 0, 30, tzinfo=UTC)


def test_throttle_does_not_publish_heartbeat_over_pending_numeric_when_due() -> None:
    clock = _FakeClock()
    sink = RecordingProgressSink()
    throttle = ProgressPersistenceThrottle(sink, clock=clock.monotonic)

    throttle.publish(_snapshot(current=1))
    throttle.publish(_snapshot(current=25))
    clock.advance(DEFAULT_PROGRESS_PERSISTENCE_INTERVAL_SECONDS)
    throttle.publish(_snapshot(current=None, unit=None, observed_offset_seconds=30))

    assert [snapshot.current for snapshot in sink.snapshots] == [1, 25]
    assert sink.snapshots[-1].heartbeat_at == datetime(2026, 7, 1, 12, 0, 30, tzinfo=UTC)


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


def test_throttle_serializes_concurrent_parser_and_heartbeat_publications() -> None:
    clock = _FakeClock()
    sink = RecordingProgressSink()
    throttle = ProgressPersistenceThrottle(sink, clock=clock.monotonic)
    barrier = threading.Barrier(3)

    def publish_numeric() -> None:
        barrier.wait()
        for value in range(1, 101):
            throttle.publish(_snapshot(current=value))

    def publish_heartbeats() -> None:
        barrier.wait()
        for offset in range(1, 101):
            throttle.publish(
                _snapshot(current=None, unit=None, observed_offset_seconds=offset)
            )

    numeric_thread = threading.Thread(target=publish_numeric)
    heartbeat_thread = threading.Thread(target=publish_heartbeats)
    numeric_thread.start()
    heartbeat_thread.start()
    barrier.wait()
    numeric_thread.join()
    heartbeat_thread.join()
    throttle.publish(_snapshot(current=101))
    throttle.close()

    assert sink.snapshots[-1].current == 101


def test_throttle_preserves_transitions_and_latest_snapshot_in_high_frequency_stream() -> None:
    clock = _FakeClock()
    sink = RecordingProgressSink()
    throttle = ProgressPersistenceThrottle(sink, clock=clock.monotonic)

    throttle.publish(_snapshot(phase=ProgressPhase.PREPARING, current=None, unit=None))
    for value in range(1, 5_000):
        throttle.publish(_snapshot(current=value))
    clock.advance(DEFAULT_PROGRESS_PERSISTENCE_INTERVAL_SECONDS)
    assert throttle.flush_due() is True
    throttle.publish(_snapshot(phase=ProgressPhase.MUXING, current=None, unit=None))
    throttle.publish(_snapshot(phase=ProgressPhase.COMPLETED, current=5_000))

    assert [snapshot.phase for snapshot in sink.snapshots] == [
        ProgressPhase.PREPARING,
        ProgressPhase.ENCODING,
        ProgressPhase.ENCODING,
        ProgressPhase.MUXING,
        ProgressPhase.COMPLETED,
    ]
    assert sink.snapshots[2].current == 4_999
    assert len(sink.snapshots) == 5


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
    observed_offset_seconds: int | None = None,
) -> ProgressSnapshot:
    now = datetime(2026, 7, 1, 12, tzinfo=UTC)
    if observed_offset_seconds is not None:
        now += timedelta(seconds=observed_offset_seconds)
    elif current is not None:
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
