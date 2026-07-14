from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable
from contextlib import suppress
from threading import Lock
from time import monotonic
from typing import Protocol

from avarch.domain.progress import TERMINAL_PROGRESS_PHASES, ProgressPhase, ProgressSnapshot

__all__ = [
    "CoalescingProgressBridge",
    "DEFAULT_PROGRESS_PERSISTENCE_INTERVAL_SECONDS",
    "NoopProgressSink",
    "ProgressPersistenceThrottle",
    "ProgressSink",
    "RecordingProgressSink",
    "publish_progress_safely",
]

DEFAULT_PROGRESS_PERSISTENCE_INTERVAL_SECONDS = 1.5


class ProgressSink(Protocol):
    def publish(self, snapshot: ProgressSnapshot) -> None: ...


class NoopProgressSink:
    def publish(self, snapshot: ProgressSnapshot) -> None:
        del snapshot


class RecordingProgressSink:
    def __init__(self) -> None:
        self._snapshots: list[ProgressSnapshot] = []

    @property
    def snapshots(self) -> tuple[ProgressSnapshot, ...]:
        return tuple(self._snapshots)

    def publish(self, snapshot: ProgressSnapshot) -> None:
        self._snapshots.append(snapshot)


ProgressConsumer = Callable[[ProgressSnapshot], Awaitable[None]]


class CoalescingProgressBridge:
    def __init__(
        self,
        consumer: ProgressConsumer,
        *,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> None:
        self._consumer = consumer
        self._loop = loop or asyncio.get_running_loop()
        self._wake = asyncio.Event()
        self._important: deque[ProgressSnapshot] = deque()
        self._latest_ordinary: ProgressSnapshot | None = None
        self._last_phase: ProgressPhase | None = None
        self._accepting = True
        self._scheduled_callbacks = 0
        self._lock = Lock()
        self._drain_task = self._loop.create_task(self._drain())

    def publish(self, snapshot: ProgressSnapshot) -> None:
        with self._lock:
            if not self._accepting:
                return
            self._scheduled_callbacks += 1
        try:
            self._loop.call_soon_threadsafe(self._enqueue, snapshot)
        except RuntimeError:
            with self._lock:
                self._scheduled_callbacks -= 1

    async def aclose(self) -> None:
        with self._lock:
            self._accepting = False
        self._wake.set()
        await self._drain_task

    def _enqueue(self, snapshot: ProgressSnapshot) -> None:
        try:
            if self._is_important(snapshot):
                self._latest_ordinary = None
                self._important.append(snapshot)
            else:
                self._latest_ordinary = snapshot
            self._last_phase = snapshot.phase
            self._wake.set()
        finally:
            with self._lock:
                self._scheduled_callbacks -= 1

    def _is_important(self, snapshot: ProgressSnapshot) -> bool:
        return (
            self._last_phase is None
            or snapshot.phase != self._last_phase
            or snapshot.phase in TERMINAL_PROGRESS_PHASES
        )

    async def _drain(self) -> None:
        while True:
            snapshot = self._pop_next()
            if snapshot is not None:
                with suppress(Exception):
                    await self._consumer(snapshot)
                continue
            if self._done_draining():
                return
            await self._wait_for_work()

    def _pop_next(self) -> ProgressSnapshot | None:
        if self._important:
            return self._important.popleft()
        if self._latest_ordinary is not None:
            snapshot = self._latest_ordinary
            self._latest_ordinary = None
            return snapshot
        return None

    def _done_draining(self) -> bool:
        with self._lock:
            return not self._accepting and self._scheduled_callbacks == 0

    async def _wait_for_work(self) -> None:
        await self._wake.wait()
        self._wake.clear()


class ProgressPersistenceThrottle:
    def __init__(
        self,
        sink: ProgressSink,
        *,
        interval_seconds: float = DEFAULT_PROGRESS_PERSISTENCE_INTERVAL_SECONDS,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._sink = sink
        self._interval_seconds = interval_seconds
        self._clock = clock
        self._pending_ordinary: ProgressSnapshot | None = None
        self._last_published_at: float | None = None
        self._last_phase: ProgressPhase | None = None
        self._closed = False

    def publish(self, snapshot: ProgressSnapshot) -> None:
        if self._closed:
            return
        if self._should_publish_immediately(snapshot):
            self._pending_ordinary = None
            self._publish_now(snapshot)
            return
        self._pending_ordinary = snapshot

    def flush_due(self) -> bool:
        if self._pending_ordinary is None or self._last_published_at is None:
            return False
        if self._clock() - self._last_published_at < self._interval_seconds:
            return False
        snapshot = self._pending_ordinary
        self._pending_ordinary = None
        return self._publish_now(snapshot)

    def close(self) -> bool:
        self._closed = True
        if self._pending_ordinary is None:
            return False
        snapshot = self._pending_ordinary
        self._pending_ordinary = None
        return self._publish_now(snapshot)

    def _should_publish_immediately(self, snapshot: ProgressSnapshot) -> bool:
        if self._last_phase is None:
            return True
        if snapshot.phase != self._last_phase:
            return True
        if snapshot.phase in TERMINAL_PROGRESS_PHASES:
            return True
        if self._last_published_at is None:
            return True
        return self._clock() - self._last_published_at >= self._interval_seconds

    def _publish_now(self, snapshot: ProgressSnapshot) -> bool:
        if not publish_progress_safely(self._sink, snapshot):
            return False
        self._last_published_at = self._clock()
        self._last_phase = snapshot.phase
        return True


def publish_progress_safely(
    sink: ProgressSink | None,
    snapshot: ProgressSnapshot,
) -> bool:
    if sink is None:
        return True
    try:
        sink.publish(snapshot)
    except Exception:
        return False
    return True
