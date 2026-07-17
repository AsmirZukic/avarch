from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime

from rich.console import RenderableType
from rich.text import Text

from avarch.application.scheduler_snapshot import (
    CapacitySummary,
    PipelineSummary,
    SchedulerRuntimeState,
    SchedulerRuntimeSummary,
    SchedulerSnapshot,
    WorkspaceSummary,
)
from avarch.application.scheduler_watch import SchedulerWatchLoop
from avarch.application.scheduler_watch_keys import KEY_CTRL_C, UnsupportedKeySource


def test_q_detaches_scheduler_watch() -> None:
    controller = _Controller()
    sink = _FakeSink()

    asyncio.run(_run_with_key("q", controller=controller, sink=sink))

    assert controller.calls == ["detach"]
    assert sink.texts == ["running"]


def test_ctrl_c_detaches_scheduler_watch() -> None:
    controller = _Controller()

    asyncio.run(_run_with_key(KEY_CTRL_C, controller=controller))

    assert controller.calls == ["detach"]


def test_p_pauses_when_scheduler_is_running() -> None:
    controller = _Controller()

    asyncio.run(_run_with_key("p", controller=controller, state=SchedulerRuntimeState.RUNNING))

    assert controller.calls == ["pause:watch"]


def test_p_resumes_when_scheduler_is_paused() -> None:
    controller = _Controller()

    asyncio.run(_run_with_key("p", controller=controller, state=SchedulerRuntimeState.PAUSED))

    assert controller.calls == ["resume"]


def test_unknown_key_takes_no_action() -> None:
    controller = _Controller()

    asyncio.run(_run_with_key("x", controller=controller))

    assert controller.calls == []


def test_unsupported_key_source_disables_shortcuts_cleanly() -> None:
    controller = _Controller()
    sink = _FakeSink()

    asyncio.run(_run_with_key_source(UnsupportedKeySource(), controller=controller, sink=sink))

    assert controller.calls == []
    assert sink.texts == ["running"]


async def _run_with_key(
    key: str,
    *,
    controller: _Controller,
    sink: _FakeSink | None = None,
    state: SchedulerRuntimeState = SchedulerRuntimeState.RUNNING,
) -> None:
    await _run_with_key_source(
        _KeySource([key]),
        controller=controller,
        sink=sink or _FakeSink(),
        state=state,
    )


async def _run_with_key_source(
    key_source: object,
    *,
    controller: _Controller,
    sink: _FakeSink,
    state: SchedulerRuntimeState = SchedulerRuntimeState.RUNNING,
) -> None:
    loop = SchedulerWatchLoop(
        snapshot_query=_Query([_snapshot(state)]),
        renderer=lambda snapshot, _width: Text(snapshot.scheduler.state.value),
        sink=sink,
        interval_seconds=1,
        terminal_width=lambda: 100,
        sleeper=lambda _interval: asyncio.sleep(0),
        stop_after_iterations=1,
        key_source=key_source,  # type: ignore[arg-type]
        watch_controller=controller,
    )
    await loop.run()


class _KeySource:
    supported = True

    def __init__(self, keys: Sequence[str]) -> None:
        self._keys = list(keys)

    def poll_key(self) -> str | None:
        if not self._keys:
            return None
        return self._keys.pop(0)


class _Controller:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def pause(self, *, reason: str | None = None) -> object:
        self.calls.append(f"pause:{reason}")
        return object()

    def resume(self) -> object:
        self.calls.append("resume")
        return object()

    def cancel(self, *, job_id: int, reason: str | None = None) -> object:
        self.calls.append(f"cancel:{job_id}:{reason}")
        return object()

    def details(self, *, job_id: int) -> None:
        del job_id
        return None

    def log_paths(self, *, job_id: int, attempt_number: int | None = None) -> object:
        del job_id, attempt_number
        return object()

    def detach(self) -> object:
        self.calls.append("detach")
        return object()


class _FakeSink:
    def __init__(self) -> None:
        self.renderables: list[RenderableType] = []
        self.texts: list[str] = []

    def render(self, renderable: RenderableType) -> None:
        self.renderables.append(renderable)
        self.texts.append(str(renderable))


class _Query:
    def __init__(self, snapshots: Sequence[SchedulerSnapshot]) -> None:
        self._snapshots = list(snapshots)

    def snapshot(self) -> SchedulerSnapshot:
        return self._snapshots.pop(0)


def _snapshot(state: SchedulerRuntimeState) -> SchedulerSnapshot:
    return SchedulerSnapshot(
        captured_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        workspace=WorkspaceSummary(root_path="/workspace"),
        scheduler=SchedulerRuntimeSummary(state=state),
        pipeline=PipelineSummary(queued=0, active=0, completed=0, failed=0),
        active_jobs=(),
        capacity=CapacitySummary(cheap_workers=0, av1an_jobs=0, file_ops=0),
        upcoming_jobs=(),
        recent_events=(),
        alerts=(),
        resources=None,
        forecast=None,
    )
