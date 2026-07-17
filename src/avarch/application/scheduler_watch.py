from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol

from rich.console import RenderableType
from rich.text import Text

from avarch.application.resource_telemetry import (
    ResourceSampler,
    ResourceTelemetryPolicy,
    apply_resource_health,
    safe_sample_resources,
)
from avarch.application.scheduler_snapshot import (
    SchedulerRuntimeState,
    SchedulerSnapshot,
    SchedulerSnapshotQuery,
    resource_telemetry_summary,
)
from avarch.application.scheduler_watch_controller import WatchController
from avarch.application.scheduler_watch_controls import SchedulerWatchControlState
from avarch.application.scheduler_watch_keys import (
    KEY_CANCEL,
    KEY_CTRL_C,
    KEY_DETAILS,
    KEY_LOGS,
    KEY_PAUSE,
    KEY_QUIT,
    KeySource,
)


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
    resource_sampler: ResourceSampler | None = None
    resource_policy: ResourceTelemetryPolicy = field(default_factory=ResourceTelemetryPolicy)
    resource_stale_after_seconds: int = 10
    key_source: KeySource | None = None
    watch_controller: WatchController | None = None
    watch_control_state: SchedulerWatchControlState = field(
        default_factory=SchedulerWatchControlState
    )

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
                snapshot = self._snapshot_with_resources(snapshot)
                should_stop = self._handle_key(snapshot)
                snapshot = self.watch_control_state.apply_to_snapshot(snapshot)
                self.sink.render(self.renderer(snapshot, width))
                if should_stop:
                    return

            iterations += 1
            if self.stop_after_iterations is not None and iterations >= self.stop_after_iterations:
                return

            await self.sleeper(self._sleep_interval(consecutive_failures))

    def _sleep_interval(self, consecutive_failures: int) -> float:
        if consecutive_failures <= self.max_consecutive_failures:
            return self.interval_seconds
        return min(self.interval_seconds * 2, 10.0)

    def _snapshot_with_resources(self, snapshot: SchedulerSnapshot) -> SchedulerSnapshot:
        if self.resource_sampler is None:
            return snapshot
        sample = safe_sample_resources(
            self.resource_sampler,
            clock=lambda: snapshot.captured_at,
        )
        sample = apply_resource_health(sample, policy=self.resource_policy)
        return snapshot.model_copy(
            update={
                "resources": resource_telemetry_summary(
                    sample,
                    captured_at=snapshot.captured_at,
                    stale_after_seconds=self.resource_stale_after_seconds,
                )
            }
        )

    def _handle_key(self, snapshot: SchedulerSnapshot) -> bool:
        self.watch_control_state.refresh(snapshot)
        if (
            self.key_source is None
            or self.watch_controller is None
            or not self.key_source.supported
        ):
            return False
        key = self.key_source.poll_key()
        if key is None:
            return False
        if key in {KEY_QUIT, KEY_CTRL_C}:
            self.watch_controller.detach()
            return True
        if key == KEY_PAUSE:
            if snapshot.scheduler.state == SchedulerRuntimeState.RUNNING:
                self.watch_controller.pause(reason="watch")
            elif snapshot.scheduler.state == SchedulerRuntimeState.PAUSED:
                self.watch_controller.resume()
            return False
        if key == KEY_DETAILS:
            self.watch_control_state.handle_details_key(controller=self.watch_controller)
            return False
        if key == KEY_LOGS:
            self.watch_control_state.handle_logs_key(controller=self.watch_controller)
            return False
        if key == KEY_CANCEL:
            if self.watch_control_state.cancel_confirmation is not None:
                self.watch_control_state.confirm_cancel(
                    controller=self.watch_controller,
                    now=snapshot.captured_at,
                )
            else:
                self.watch_control_state.handle_key(
                    key,
                    snapshot=snapshot,
                    now=snapshot.captured_at,
                )
        return False


def validate_live_watch_terminal(*, stdout_is_tty: bool) -> None:
    if not stdout_is_tty:
        raise SchedulerWatchModeError("Live scheduler watch requires a TTY. Use --once or --json.")


def _failure_renderable(exc: Exception, consecutive_failures: int) -> Text:
    if consecutive_failures > 3:
        return Text(f"Scheduler snapshot unavailable after repeated failures: {exc}", style="red")
    return Text(f"Scheduler snapshot unavailable: {exc}", style="yellow")
