from __future__ import annotations

from datetime import datetime
from typing import Protocol


class SchedulerControlWorkflowError(RuntimeError):
    pass


class SchedulerControlStore(Protocol):
    def pause(self, *, now: datetime) -> None: ...

    def resume(self, *, now: datetime) -> None: ...

    def drain(self, *, now: datetime) -> None: ...

    def stop(self, *, now: datetime) -> None: ...


def pause_scheduler(
    store: SchedulerControlStore,
    *,
    now: datetime,
) -> None:
    store.pause(now=now)


def resume_scheduler(store: SchedulerControlStore, *, now: datetime) -> None:
    store.resume(now=now)


def drain_scheduler(
    store: SchedulerControlStore,
    *,
    now: datetime,
) -> None:
    store.drain(now=now)


def stop_scheduler(
    store: SchedulerControlStore,
    *,
    now: datetime,
) -> None:
    store.stop(now=now)
