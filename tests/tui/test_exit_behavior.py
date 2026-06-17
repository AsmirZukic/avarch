from __future__ import annotations

import asyncio
from pathlib import Path

from avarch.tui.app import AvarchTuiApp, RouteContent
from avarch.tui.models.bootstrap import BootstrapState, BootstrapStatus
from avarch.tui.models.common import UiRevision
from avarch.tui.models.dashboard import DashboardSnapshot, QueueTotals, SchedulerSummary


def test_quit_with_owned_scheduler_opens_exit_modal() -> None:
    async def run() -> None:
        host = FakeExitSchedulerHost(owned=True)
        app = AvarchTuiApp(backend=FakeDashboardBackend(), scheduler_host=host)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.action_request_quit()
            assert "TUI-owned scheduler is running" in _active_text(app)
            assert host.drain_calls == 0
            assert host.stop_calls == 0

    asyncio.run(run())


def test_drain_and_exit_waits_for_scheduler() -> None:
    async def run() -> None:
        host = FakeExitSchedulerHost(owned=True)
        app = AvarchTuiApp(backend=FakeDashboardBackend(), scheduler_host=host)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.drain_and_exit()
            assert host.drain_calls == 1
            assert host.wait_calls == 1
            assert host.stop_calls == 0

    asyncio.run(run())


def test_stop_and_exit_interrupts_safely() -> None:
    async def run() -> None:
        host = FakeExitSchedulerHost(owned=True)
        app = AvarchTuiApp(backend=FakeDashboardBackend(), scheduler_host=host)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.stop_and_exit()
            assert host.stop_calls == 1
            assert host.wait_calls == 1
            assert host.drain_calls == 0

    asyncio.run(run())


def test_quit_does_not_affect_external_scheduler() -> None:
    async def run() -> None:
        host = FakeExitSchedulerHost(owned=False)
        app = AvarchTuiApp(backend=FakeDashboardBackend(), scheduler_host=host)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.action_request_quit()
            assert host.drain_calls == 0
            assert host.stop_calls == 0
            assert host.wait_calls == 0

    asyncio.run(run())


class FakeExitSchedulerHost:
    def __init__(self, *, owned: bool) -> None:
        self._owned = owned
        self.drain_calls = 0
        self.stop_calls = 0
        self.wait_calls = 0

    @property
    def owned(self) -> bool:
        return self._owned

    async def request_drain(self) -> None:
        self.drain_calls += 1
        self._owned = False

    async def request_stop(self) -> None:
        self.stop_calls += 1
        self._owned = False

    async def wait_finished(self) -> object:
        self.wait_calls += 1
        return None


class FakeDashboardBackend:
    async def get_bootstrap_status(self) -> BootstrapStatus:
        return BootstrapStatus(
            state=BootstrapState.READY,
            config_path=Path("/tmp/avarch.toml"),
            data_dir=Path("/tmp/.avarch"),
            database_url="sqlite:////tmp/.avarch/avarch.db",
        )

    async def initialize_local_state(self) -> None:
        pass

    async def get_dashboard_snapshot(self) -> DashboardSnapshot:
        return DashboardSnapshot(
            revision=UiRevision(
                scheduler_generation=0,
                newest_job_updated_at=None,
                newest_attempt_updated_at=None,
                newest_validation_created_at=None,
                newest_promotion_updated_at=None,
            ),
            scheduler=SchedulerSummary(
                mode="running",
                lease_state="active",
                runner_id="external",
                heartbeat_at=None,
                lease_expires_at=None,
                control_generation=0,
                acknowledged_generation=0,
                cancel_pending=0,
                hold_pending=0,
            ),
            queue_totals=QueueTotals(),
            active_jobs=(),
            recent_failures=(),
            promotion_ready=(),
        )


def _active_text(app: AvarchTuiApp) -> str:
    return app.query_one("#active-screen", RouteContent).content_text
