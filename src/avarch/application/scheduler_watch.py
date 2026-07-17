from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from rich.console import RenderableType
from rich.text import Text

from avarch.application.scheduler_snapshot import SchedulerSnapshot, SchedulerSnapshotQuery


class SchedulerWatchModeError(RuntimeError):
    pass


class SchedulerWatchRenderSink(Protocol):
    def render(self, renderable: RenderableType) -> None:
        pass


SnapshotRenderer = Callable[[SchedulerSnapshot, int], RenderableType]
Sleeper = Callable[[float], Awaitable[None]]
TerminalWidth = Callable[[], int]


@dataclass(slots=True)
class SchedulerWatchLoop:
    snapshot_query: SchedulerSnapshotQuery
    renderer: SnapshotRenderer
    sink: SchedulerWatchRenderSink
    interval_seconds: float
    terminal_width: TerminalWidth
    sleeper: Sleeper = asyncio.sleep
    max_consecutive_failures: int = 3
    stop_after_iterations: int | None = None

    async def run(self) -> None:
        iterations = 0
        consecutive_failures = 0
        while True:
            width = self.terminal_width()
            try:
                snapshot = self.snapshot_query.snapshot()
            except KeyboardInterrupt:
                return
            except Exception as exc:
                consecutive_failures += 1
                self.sink.render(_failure_renderable(exc, consecutive_failures))
            else:
                consecutive_failures = 0
                self.sink.render(self.renderer(snapshot, width))

            iterations += 1
            if self.stop_after_iterations is not None and iterations >= self.stop_after_iterations:
                return

            await self.sleeper(self._sleep_interval(consecutive_failures))

    def _sleep_interval(self, consecutive_failures: int) -> float:
        if consecutive_failures <= self.max_consecutive_failures:
            return self.interval_seconds
        return min(self.interval_seconds * 2, 10.0)


def validate_live_watch_terminal(*, stdout_is_tty: bool) -> None:
    if not stdout_is_tty:
        raise SchedulerWatchModeError("Live scheduler watch requires a TTY. Use --once or --json.")


def _failure_renderable(exc: Exception, consecutive_failures: int) -> Text:
    if consecutive_failures > 3:
        return Text(f"Scheduler snapshot unavailable after repeated failures: {exc}", style="red")
    return Text(f"Scheduler snapshot unavailable: {exc}", style="yellow")
