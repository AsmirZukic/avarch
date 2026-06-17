from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

from sqlmodel import Session

from avarch.config import AppConfig
from avarch.db import create_db_engine
from avarch.models.scheduler import SchedulerMode
from avarch.scheduler import (
    SchedulerAlreadyRunningError,
    SchedulerRunSummary,
    drain_scheduler,
    new_runner_id,
    run_scheduler,
    scheduler_status,
    stop_scheduler,
    utc_now,
)

SchedulerRunner = Callable[..., Awaitable[SchedulerRunSummary]]


class SchedulerOwnership(Protocol):
    @property
    def owned(self) -> bool:
        ...


class TuiSchedulerHost:
    def __init__(
        self,
        *,
        config: AppConfig,
        runner: SchedulerRunner = run_scheduler,
        runner_id: str | None = None,
    ) -> None:
        self.config = config
        self.runner = runner
        self.runner_id = runner_id or new_runner_id()
        self._task: asyncio.Future[SchedulerRunSummary] | None = None

    @property
    def owned(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self, *, resume: bool = False) -> None:
        if self.owned:
            return
        self._reject_live_external_scheduler()
        self._task = asyncio.ensure_future(
            self.runner(config=self.config, runner_id=self.runner_id, resume=resume)
        )
        await asyncio.sleep(0)
        if self._task.done():
            self._task.result()

    async def request_drain(self) -> None:
        await asyncio.to_thread(self._request_control, "drain")

    async def request_stop(self) -> None:
        await asyncio.to_thread(self._request_control, "stop")

    async def wait_finished(self) -> SchedulerRunSummary | None:
        if self._task is None:
            return None
        return await self._task

    def external_scheduler_active(self) -> bool:
        engine = create_db_engine(self.config.database.url)
        with Session(engine) as session:
            status = scheduler_status(session, now=utc_now())
        return (
            status.lease_state == "active"
            and status.runner_id is not None
            and status.runner_id != self.runner_id
        )

    def _reject_live_external_scheduler(self) -> None:
        if self.external_scheduler_active():
            raise SchedulerAlreadyRunningError("Another scheduler lease is still active.")

    def _request_control(self, action: str) -> None:
        engine = create_db_engine(self.config.database.url)
        with Session(engine) as session, session.begin():
            if action == "drain":
                drain_scheduler(session, now=utc_now(), reason="tui exit")
            elif action == "stop":
                stop_scheduler(session, now=utc_now(), reason="tui exit")
            else:
                raise ValueError(f"Unsupported scheduler control: {action}")


def scheduler_ownership_label(
    *,
    host: SchedulerOwnership | None,
    mode: str,
    lease_state: str,
    runner_id: str | None,
) -> str:
    if host is not None and host.owned:
        return "Scheduler: Running in this TUI"
    if lease_state == "active" and runner_id is not None:
        return "Scheduler: Running externally"
    if mode == SchedulerMode.RUNNING.value:
        return "Scheduler: No live scheduler"
    return f"Scheduler: {mode}"
