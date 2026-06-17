from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from avarch.tui.app import AvarchTuiApp, RouteContent
from avarch.tui.messages import RefreshCompleted
from avarch.tui.models.bootstrap import BootstrapState, BootstrapStatus
from avarch.tui.models.common import UiRevision
from avarch.tui.models.dashboard import DashboardSnapshot, QueueTotals, SchedulerSummary


def test_refresh_does_not_overlap_same_screen() -> None:
    async def run() -> None:
        backend = FakeRefreshBackend((_snapshot(1, pending=1), _snapshot(2, pending=2)))
        backend.block_refreshes = True
        app = AvarchTuiApp(backend=backend)
        async with app.run_test(size=(100, 30)) as _pilot:
            first = asyncio.create_task(app.refresh_active_screen())
            await backend.started.wait()
            second = asyncio.create_task(app.refresh_active_screen())
            await asyncio.sleep(0)
            assert backend.max_concurrent == 1
            backend.release.set()
            await asyncio.gather(first, second)
            assert backend.calls == 2
            assert backend.max_concurrent == 1

    asyncio.run(run())


def test_latest_refresh_result_wins() -> None:
    async def run() -> None:
        backend = FakeRefreshBackend((_snapshot(1, pending=1), _snapshot(2, pending=2)))
        backend.block_refreshes = True
        app = AvarchTuiApp(backend=backend)
        async with app.run_test(size=(100, 30)) as _pilot:
            first = asyncio.create_task(app.refresh_active_screen())
            await backend.started.wait()
            second = asyncio.create_task(app.refresh_active_screen())
            backend.release.set()
            await asyncio.gather(first, second)
            text = app.query_one("#active-screen", RouteContent).content_text
            assert "Pending: 2" in text
            assert "Pending: 1" not in text

    asyncio.run(run())


def test_unchanged_revision_skips_rerender() -> None:
    async def run() -> None:
        revision = _revision(1)
        backend = FakeRefreshBackend(
            (
                _snapshot_from_revision(revision, pending=1),
                _snapshot_from_revision(revision, pending=99),
            )
        )
        app = AvarchTuiApp(backend=backend)
        async with app.run_test(size=(100, 30)) as _pilot:
            await app.refresh_active_screen()
            await app.refresh_active_screen()
            assert app.snapshot_render_count == 1
            assert "Pending: 1" in app.query_one("#active-screen", RouteContent).content_text
            completed = [
                event for event in app.refresh_events if isinstance(event, RefreshCompleted)
            ]
            assert completed[-1].rendered is False

    asyncio.run(run())


def test_manual_refresh_runs_immediately() -> None:
    async def run() -> None:
        backend = FakeRefreshBackend((_snapshot(1, pending=4),))
        app = AvarchTuiApp(backend=backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("r")
            await pilot.pause()
            assert backend.calls == 1
            assert "Pending: 4" in app.query_one("#active-screen", RouteContent).content_text

    asyncio.run(run())


def test_refresh_error_keeps_previous_snapshot() -> None:
    async def run() -> None:
        backend = FakeRefreshBackend((_snapshot(1, pending=3), RuntimeError("database busy")))
        app = AvarchTuiApp(backend=backend)
        async with app.run_test(size=(100, 30)) as _pilot:
            await app.refresh_active_screen()
            previous = app.query_one("#active-screen", RouteContent).content_text
            await app.refresh_active_screen()
            assert app.query_one("#active-screen", RouteContent).content_text == previous
            assert app.last_refresh_error is not None
            assert app.last_refresh_error.summary == "database busy"

    asyncio.run(run())


class FakeRefreshBackend:
    def __init__(self, results: tuple[DashboardSnapshot | Exception, ...]) -> None:
        self.results = list(results)
        self.calls = 0
        self.active_calls = 0
        self.max_concurrent = 0
        self.block_refreshes = False
        self.started = asyncio.Event()
        self.release = asyncio.Event()

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
        self.calls += 1
        self.active_calls += 1
        self.max_concurrent = max(self.max_concurrent, self.active_calls)
        self.started.set()
        if self.block_refreshes and self.calls == 1:
            await self.release.wait()
        try:
            result = self.results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        finally:
            self.active_calls -= 1


def _snapshot(generation: int, *, pending: int) -> DashboardSnapshot:
    return _snapshot_from_revision(_revision(generation), pending=pending)


def _snapshot_from_revision(revision: UiRevision, *, pending: int) -> DashboardSnapshot:
    return DashboardSnapshot(
        revision=revision,
        scheduler=SchedulerSummary(
            mode="running",
            lease_state="active",
            runner_id="runner",
            heartbeat_at=None,
            lease_expires_at=None,
            control_generation=revision.scheduler_generation,
            acknowledged_generation=revision.scheduler_generation,
            cancel_pending=0,
            hold_pending=0,
        ),
        queue_totals=QueueTotals(pending=pending),
        active_jobs=(),
        recent_failures=(),
        promotion_ready=(),
    )


def _revision(generation: int) -> UiRevision:
    return UiRevision(
        scheduler_generation=generation,
        newest_job_updated_at=datetime(2026, 1, generation, tzinfo=UTC),
        newest_attempt_updated_at=None,
        newest_validation_created_at=None,
        newest_promotion_updated_at=None,
    )
