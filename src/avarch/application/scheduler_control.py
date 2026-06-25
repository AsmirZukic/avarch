from __future__ import annotations

from datetime import datetime
from typing import Protocol


class SchedulerControlWorkflowError(RuntimeError):
    pass


class SchedulerControlStore(Protocol):
    def pause(self, *, now: datetime, reason: str | None) -> None: ...

    def resume(self, *, now: datetime) -> None: ...

    def drain(self, *, now: datetime, reason: str | None) -> None: ...

    def stop(self, *, now: datetime, reason: str | None) -> None: ...


def pause_scheduler(
    store: SchedulerControlStore,
    *,
    now: datetime,
    reason: str | None = None,
) -> None:
    store.pause(now=now, reason=reason)


def resume_scheduler(store: SchedulerControlStore, *, now: datetime) -> None:
    store.resume(now=now)


def drain_scheduler(
    store: SchedulerControlStore,
    *,
    now: datetime,
    reason: str | None = None,
) -> None:
    store.drain(now=now, reason=reason)


def stop_scheduler(
    store: SchedulerControlStore,
    *,
    now: datetime,
    reason: str | None = None,
) -> None:
    store.stop(now=now, reason=reason)
