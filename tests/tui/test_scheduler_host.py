from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session

from avarch.config import AppConfig
from avarch.db import create_db_engine, create_db_schema
from avarch.models.db import SchedulerState
from avarch.models.scheduler import SchedulerMode
from avarch.scheduler import SchedulerAlreadyRunningError, SchedulerRunSummary
from avarch.tui.scheduler_host import TuiSchedulerHost, scheduler_ownership_label


def test_start_uses_existing_scheduler_runner(tmp_path: Path) -> None:
    async def run() -> None:
        runner = BlockingRunner()
        config = _initialized_config(tmp_path)
        host = TuiSchedulerHost(config=config, runner=runner, runner_id="tui-runner")

        await host.start()

        assert runner.calls == [
            {"config": config, "runner_id": "tui-runner", "resume": False}
        ]
        assert host.owned is True
        runner.release.set()
        await host.wait_finished()
        assert host.owned is False

    asyncio.run(run())


def test_start_rejects_live_external_scheduler(tmp_path: Path) -> None:
    config = _config(tmp_path)
    engine = create_db_engine(config.database.url)
    create_db_schema(engine)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        session.add(
            SchedulerState(
                id=1,
                mode=SchedulerMode.RUNNING,
                runner_id="external",
                heartbeat_at=now,
                lease_expires_at=now + timedelta(seconds=60),
                updated_at=now,
            )
        )
    host = TuiSchedulerHost(config=config, runner=BlockingRunner(), runner_id="tui-runner")

    async def run() -> None:
        with pytest.raises(SchedulerAlreadyRunningError):
            await host.start()

    asyncio.run(run())


def test_owned_scheduler_is_identified(tmp_path: Path) -> None:
    async def run() -> None:
        runner = BlockingRunner()
        host = TuiSchedulerHost(
            config=_initialized_config(tmp_path),
            runner=runner,
            runner_id="tui-runner",
        )
        await host.start()

        assert scheduler_ownership_label(
            host=host,
            mode="running",
            lease_state="active",
            runner_id="tui-runner",
        ) == "Scheduler: Running in this TUI"
        runner.release.set()
        await host.wait_finished()

    asyncio.run(run())


def test_external_scheduler_is_not_owned() -> None:
    assert (
        scheduler_ownership_label(
            host=None,
            mode="running",
            lease_state="active",
            runner_id="external",
        )
        == "Scheduler: Running externally"
    )


class BlockingRunner:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        *,
        config: AppConfig,
        runner_id: str,
        resume: bool = False,
    ) -> SchedulerRunSummary:
        self.calls.append({"config": config, "runner_id": runner_id, "resume": resume})
        await self.release.wait()
        return SchedulerRunSummary(completed=0, failed=0, skipped=0, idle=True)


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig.model_validate({"database": {"url": f"sqlite:///{tmp_path / 'avarch.db'}"}})


def _initialized_config(tmp_path: Path) -> AppConfig:
    config = _config(tmp_path)
    engine = create_db_engine(config.database.url)
    create_db_schema(engine)
    return config
