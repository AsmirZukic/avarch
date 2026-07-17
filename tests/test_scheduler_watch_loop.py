from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest
from rich.console import RenderableType
from rich.text import Text

from avarch.application.resource_telemetry import (
    NullResourceSampler,
    ResourceMetric,
    ResourceSample,
)
from avarch.application.scheduler_snapshot import (
    CapacitySummary,
    PipelineSummary,
    SchedulerRuntimeState,
    SchedulerRuntimeSummary,
    SchedulerSnapshot,
    WorkspaceSummary,
)
from avarch.application.scheduler_watch import (
    SchedulerWatchLoop,
    SchedulerWatchModeError,
    validate_live_watch_terminal,
)


def test_scheduler_watch_loop_refreshes_snapshot_at_interval() -> None:
    query = _FakeQuery([_snapshot(0), _snapshot(1), _snapshot(2)])
    sink = _FakeSink()
    sleeps: list[float] = []

    async def sleeper(interval: float) -> None:
        sleeps.append(interval)

    async def scenario() -> None:
        loop = SchedulerWatchLoop(
            snapshot_query=query,
            renderer=lambda snapshot, width: Text(f"{snapshot.captured_at.second}:{width}"),
            sink=sink,
            interval_seconds=1.5,
            terminal_width=lambda: 120,
            sleeper=sleeper,
            stop_after_iterations=3,
        )
        await loop.run()

    asyncio.run(scenario())

    assert query.calls == 3
    assert sleeps == [1.5, 1.5]
    assert sink.texts == ["0:120", "1:120", "2:120"]


def test_scheduler_watch_loop_does_not_overlap_slow_queries() -> None:
    query = _NonOverlappingQuery([_snapshot(0), _snapshot(1)])
    sink = _FakeSink()

    async def scenario() -> None:
        loop = SchedulerWatchLoop(
            snapshot_query=query,
            renderer=lambda snapshot, _width: Text(str(snapshot.captured_at.second)),
            sink=sink,
            interval_seconds=0.01,
            terminal_width=lambda: 100,
            sleeper=lambda _interval: asyncio.sleep(0),
            stop_after_iterations=2,
        )
        await loop.run()

    asyncio.run(scenario())

    assert query.overlapped is False
    assert sink.texts == ["0", "1"]


def test_scheduler_watch_loop_exits_on_keyboard_interrupt_without_mutating_scheduler() -> None:
    query = _FakeQuery([KeyboardInterrupt()])
    sink = _FakeSink()

    async def scenario() -> None:
        loop = SchedulerWatchLoop(
            snapshot_query=query,
            renderer=lambda _snapshot, _width: Text("unused"),
            sink=sink,
            interval_seconds=1,
            terminal_width=lambda: 100,
            sleeper=lambda _interval: asyncio.sleep(0),
        )
        await loop.run()

    asyncio.run(scenario())

    assert query.calls == 1
    assert query.scheduler_mutations == 0
    assert sink.texts == []


def test_scheduler_watch_loop_renders_transient_failures_and_retries() -> None:
    query = _FakeQuery([RuntimeError("locked"), _snapshot(1)])
    sink = _FakeSink()
    sleeps: list[float] = []

    async def scenario() -> None:
        loop = SchedulerWatchLoop(
            snapshot_query=query,
            renderer=lambda _snapshot, _width: Text("ok"),
            sink=sink,
            interval_seconds=1,
            terminal_width=lambda: 100,
            sleeper=lambda interval: _record_sleep(sleeps, interval),
            stop_after_iterations=2,
        )
        await loop.run()

    asyncio.run(scenario())

    assert "Scheduler snapshot unavailable: locked" in sink.texts[0]
    assert sink.texts[-1] == "ok"
    assert sleeps == [1]


def test_scheduler_watch_loop_uses_bounded_retry_sleep_for_repeated_failures() -> None:
    query = _FakeQuery([RuntimeError("locked")] * 5)
    sink = _FakeSink()
    sleeps: list[float] = []

    async def scenario() -> None:
        loop = SchedulerWatchLoop(
            snapshot_query=query,
            renderer=lambda _snapshot, _width: Text("unused"),
            sink=sink,
            interval_seconds=3,
            terminal_width=lambda: 100,
            sleeper=lambda interval: _record_sleep(sleeps, interval),
            max_consecutive_failures=2,
            stop_after_iterations=5,
        )
        await loop.run()

    asyncio.run(scenario())

    assert sleeps == [3, 3, 6, 6]
    assert "repeated failures" in sink.texts[-1]


def test_scheduler_watch_loop_uses_latest_terminal_width() -> None:
    query = _FakeQuery([_snapshot(0), _snapshot(1)])
    widths = iter([90, 140])
    sink = _FakeSink()

    async def scenario() -> None:
        loop = SchedulerWatchLoop(
            snapshot_query=query,
            renderer=lambda _snapshot, width: Text(str(width)),
            sink=sink,
            interval_seconds=1,
            terminal_width=lambda: next(widths),
            sleeper=lambda _interval: asyncio.sleep(0),
            stop_after_iterations=2,
        )
        await loop.run()

    asyncio.run(scenario())

    assert sink.texts == ["90", "140"]


def test_scheduler_watch_loop_merges_resource_telemetry_before_rendering() -> None:
    captured_at = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    query = _FakeQuery([_snapshot(0)])
    sink = _FakeSink()

    class FixedSampler:
        def sample(self) -> ResourceSample:
            return ResourceSample(
                sampled_at=captured_at,
                metrics=(
                    ResourceMetric(name="cpu", value=95.0, unit="percent"),
                        ResourceMetric(
                            name="memory",
                            value=7 * 1024**3,
                            total=10 * 1024**3,
                            unit="bytes",
                        ),
                    ResourceMetric(
                        name="output write rate",
                        value=84 * 1024**2,
                        unit="bytes_per_second",
                    ),
                ),
            )

    async def scenario() -> None:
        loop = SchedulerWatchLoop(
            snapshot_query=query,
            renderer=lambda snapshot, _width: Text(
                f"{snapshot.resources.health}:{snapshot.resources.metrics[0].value}"  # type: ignore[union-attr]
            ),
            sink=sink,
            interval_seconds=1,
            terminal_width=lambda: 100,
            sleeper=lambda _interval: asyncio.sleep(0),
            stop_after_iterations=1,
            resource_sampler=FixedSampler(),
        )
        await loop.run()

    asyncio.run(scenario())

    assert sink.texts == ["ok:95.0"]


def test_scheduler_watch_loop_marks_resource_telemetry_stale() -> None:
    query = _FakeQuery([_snapshot(20)])
    sink = _FakeSink()

    class OldSampler:
        def sample(self) -> ResourceSample:
            return ResourceSample(
                sampled_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
                metrics=(ResourceMetric(name="cpu", value=10.0, unit="percent"),),
            )

    async def scenario() -> None:
        loop = SchedulerWatchLoop(
            snapshot_query=query,
            renderer=lambda snapshot, _width: Text(str(snapshot.resources.stale)),  # type: ignore[union-attr]
            sink=sink,
            interval_seconds=1,
            terminal_width=lambda: 100,
            sleeper=lambda _interval: asyncio.sleep(0),
            stop_after_iterations=1,
            resource_sampler=OldSampler(),
            resource_stale_after_seconds=10,
        )
        await loop.run()

    asyncio.run(scenario())

    assert sink.texts == ["True"]


def test_scheduler_watch_loop_renders_unsupported_resource_telemetry() -> None:
    query = _FakeQuery([_snapshot(0)])
    sink = _FakeSink()

    async def scenario() -> None:
        loop = SchedulerWatchLoop(
            snapshot_query=query,
            renderer=lambda snapshot, _width: Text(str(snapshot.resources.health)),  # type: ignore[union-attr]
            sink=sink,
            interval_seconds=1,
            terminal_width=lambda: 100,
            sleeper=lambda _interval: asyncio.sleep(0),
            stop_after_iterations=1,
            resource_sampler=NullResourceSampler(reason="unsupported_platform"),
        )
        await loop.run()

    asyncio.run(scenario())

    assert sink.texts == ["unavailable"]


def test_live_watch_rejects_non_tty() -> None:
    with pytest.raises(SchedulerWatchModeError, match="Use --once or --json"):
        validate_live_watch_terminal(stdout_is_tty=False)


async def _record_sleep(sleeps: list[float], interval: float) -> None:
    sleeps.append(interval)


class _FakeSink:
    def __init__(self) -> None:
        self.renderables: list[RenderableType] = []
        self.texts: list[str] = []

    def render(self, renderable: RenderableType) -> None:
        self.renderables.append(renderable)
        self.texts.append(str(renderable))


class _FakeQuery:
    def __init__(self, results: Sequence[SchedulerSnapshot | BaseException]) -> None:
        self._results = results
        self.calls = 0
        self.scheduler_mutations = 0

    def snapshot(self) -> SchedulerSnapshot:
        result = self._results[self.calls]
        self.calls += 1
        if isinstance(result, BaseException):
            raise result
        return result


class _NonOverlappingQuery(_FakeQuery):
    def __init__(self, results: list[SchedulerSnapshot]) -> None:
        super().__init__(results)
        self._inside = False
        self.overlapped = False

    def snapshot(self) -> SchedulerSnapshot:
        if self._inside:
            self.overlapped = True
        self._inside = True
        try:
            return super().snapshot()
        finally:
            self._inside = False


def _snapshot(second: int) -> SchedulerSnapshot:
    return SchedulerSnapshot(
        captured_at=datetime(2026, 7, 17, 12, 0, second, tzinfo=UTC),
        workspace=WorkspaceSummary(root_path="/workspace"),
        scheduler=SchedulerRuntimeSummary(state=SchedulerRuntimeState.RUNNING),
        pipeline=PipelineSummary(queued=0, active=0, completed=0, failed=0),
        active_jobs=(),
        capacity=CapacitySummary(cheap_workers=0, av1an_jobs=0, file_ops=0),
        upcoming_jobs=(),
        recent_events=(),
        alerts=(),
        resources=None,
        forecast=None,
    )
