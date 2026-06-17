from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session
from textual.app import App, ComposeResult

from avarch.config import AppConfig
from avarch.db import create_db_engine, create_db_schema
from avarch.models.db import SchedulerState
from avarch.models.scheduler import SchedulerMode
from avarch.tui.backend import (
    LocalTuiBackend,
    SchedulerControlRequest,
    SchedulerControlResult,
)
from avarch.tui.models.dashboard import SchedulerSummary
from avarch.tui.widgets.scheduler_panel import SchedulerPanel


def test_no_scheduler_shows_start() -> None:
    async def run() -> None:
        app = SchedulerPanelTestApp(_scheduler(mode="unknown", lease_state="inactive"))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert "Start Scheduler" in app.panel.content_text

    asyncio.run(run())


def test_running_scheduler_shows_pause_drain_stop() -> None:
    async def run() -> None:
        app = SchedulerPanelTestApp(_scheduler(mode="running"))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            text = app.panel.content_text
            assert "Pause" in text
            assert "Drain" in text
            assert "Stop" in text

    asyncio.run(run())


def test_paused_scheduler_shows_resume_and_stop() -> None:
    async def run() -> None:
        app = SchedulerPanelTestApp(_scheduler(mode="paused"))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            text = app.panel.content_text
            assert "Resume" in text
            assert "Stop" in text

    asyncio.run(run())


def test_pause_confirmation_explains_active_work_finishes() -> None:
    panel = SchedulerPanel(_scheduler(mode="running"), backend=FakeSchedulerBackend())

    panel.prepare_action("pause")

    assert "Active work will finish" in panel.content_text
    assert "No new jobs will start until resumed" in panel.content_text


def test_drain_confirmation_explains_scheduler_exit() -> None:
    panel = SchedulerPanel(_scheduler(mode="running"), backend=FakeSchedulerBackend())

    panel.prepare_action("drain")

    assert "The scheduler will exit after active work completes" in panel.content_text


def test_stop_confirmation_explains_interruption() -> None:
    panel = SchedulerPanel(_scheduler(mode="running"), backend=FakeSchedulerBackend())

    panel.prepare_action("stop")

    assert "will be interrupted safely" in panel.content_text
    assert "Jobs will not be canceled" in panel.content_text


def test_control_action_calls_backend_once() -> None:
    async def run() -> None:
        backend = FakeSchedulerBackend()
        app = SchedulerPanelTestApp(_scheduler(mode="running"), backend=backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.panel.prepare_action("pause")
            await app.panel.confirm_pending_action()
            assert backend.requests == [SchedulerControlRequest(action="pause", reason="tui")]

    asyncio.run(run())


def test_control_conflict_refreshes_status() -> None:
    async def run() -> None:
        backend = FakeSchedulerBackend(failure=RuntimeError("Cannot pause while paused."))
        app = SchedulerPanelTestApp(
            _scheduler(mode="running"),
            backend=backend,
            refreshed=_scheduler(mode="paused"),
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.panel.prepare_action("pause")
            await app.panel.confirm_pending_action()
            assert app.refresh_calls == 1
            assert "Cannot pause while paused" in app.panel.content_text
            assert "Mode: PAUSED" in app.panel.content_text

    asyncio.run(run())


def test_backend_pause_control_uses_scheduler_state(tmp_path: Path) -> None:
    database_url = f"sqlite:///{tmp_path / 'avarch.db'}"
    engine = create_db_engine(database_url)
    create_db_schema(engine)
    backend = LocalTuiBackend(
        config=AppConfig.model_validate({"database": {"url": database_url}}),
        config_path=tmp_path / "avarch.toml",
    )

    result = asyncio.run(
        backend.request_scheduler_control(SchedulerControlRequest(action="pause", reason="test"))
    )

    with Session(engine) as session:
        state = session.get(SchedulerState, 1)

    assert result.message == "Scheduler pause requested."
    assert state is not None
    assert state.mode == SchedulerMode.PAUSED
    assert state.control_reason == "test"


class SchedulerPanelTestApp(App[None]):
    def __init__(
        self,
        scheduler: SchedulerSummary,
        *,
        backend: FakeSchedulerBackend | None = None,
        refreshed: SchedulerSummary | None = None,
    ) -> None:
        super().__init__()
        self.backend = backend or FakeSchedulerBackend()
        self.refreshed = refreshed or scheduler
        self.refresh_calls = 0
        self.panel = SchedulerPanel(
            scheduler,
            backend=self.backend,
            refresh_status=self.refresh_status,
        )

    def compose(self) -> ComposeResult:
        yield self.panel

    async def refresh_status(self) -> SchedulerSummary:
        self.refresh_calls += 1
        return self.refreshed


class FakeSchedulerBackend:
    def __init__(self, failure: Exception | None = None) -> None:
        self.failure = failure
        self.requests: list[SchedulerControlRequest] = []

    async def request_scheduler_control(
        self,
        request: SchedulerControlRequest,
    ) -> SchedulerControlResult:
        self.requests.append(request)
        if self.failure is not None:
            raise self.failure
        return SchedulerControlResult(message=f"{request.action} requested")


def _scheduler(
    *,
    mode: str,
    lease_state: str = "active",
) -> SchedulerSummary:
    return SchedulerSummary(
        mode=mode,
        lease_state=lease_state,
        runner_id="runner" if lease_state == "active" else None,
        heartbeat_at=datetime.now(UTC),
        lease_expires_at=None,
        control_generation=1,
        acknowledged_generation=1,
        cancel_pending=0,
        hold_pending=0,
    )
