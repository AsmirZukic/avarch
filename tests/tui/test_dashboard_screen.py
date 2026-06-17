from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from avarch.tui.app import AvarchTuiApp, RouteContent
from avarch.tui.models.bootstrap import BootstrapState, BootstrapStatus
from avarch.tui.models.common import UiRevision
from avarch.tui.models.dashboard import (
    DashboardJobSummary,
    DashboardSnapshot,
    QueueTotals,
    SchedulerSummary,
)
from avarch.tui.state import TuiRoute


def test_dashboard_shows_scheduler_mode() -> None:
    async def run() -> None:
        app = AvarchTuiApp(backend=FakeDashboardBackend(_snapshot()))
        async with app.run_test(size=(100, 30)) as _pilot:
            await app.refresh_active_screen()
            assert "Mode: RUNNING" in _active_text(app)
            assert "Lease: active" in _active_text(app)

    asyncio.run(run())


def test_dashboard_shows_queue_totals() -> None:
    async def run() -> None:
        app = AvarchTuiApp(
            backend=FakeDashboardBackend(
                _snapshot(totals=QueueTotals(pending=12, running=3, failed=2, completed=87))
            )
        )
        async with app.run_test(size=(100, 30)) as _pilot:
            await app.refresh_active_screen()
            text = _active_text(app)
            assert "Pending: 12" in text
            assert "Running: 3" in text
            assert "Failed: 2" in text
            assert "Completed: 87" in text

    asyncio.run(run())


def test_dashboard_shows_active_jobs() -> None:
    active = _job(1, "Movie A.mkv", status="running", stage="encode")

    async def run() -> None:
        app = AvarchTuiApp(backend=FakeDashboardBackend(_snapshot(active_jobs=(active,))))
        async with app.run_test(size=(100, 30)) as _pilot:
            await app.refresh_active_screen()
            text = _active_text(app)
            assert "Active work" in text
            assert "Movie A.mkv" in text
            assert "RUNNING encode" in text

    asyncio.run(run())


def test_dashboard_shows_recent_failures() -> None:
    failure = _job(2, "Movie B.mkv", status="failed", stage="validate")

    async def run() -> None:
        app = AvarchTuiApp(backend=FakeDashboardBackend(_snapshot(recent_failures=(failure,))))
        async with app.run_test(size=(100, 30)) as _pilot:
            await app.refresh_active_screen()
            text = _active_text(app)
            assert "Recent failures" in text
            assert "Movie B.mkv" in text

    asyncio.run(run())


def test_dashboard_shows_promotion_ready_jobs() -> None:
    ready = _job(3, "Movie C.mkv", status="validated", stage="promote")

    async def run() -> None:
        app = AvarchTuiApp(backend=FakeDashboardBackend(_snapshot(promotion_ready=(ready,))))
        async with app.run_test(size=(100, 30)) as _pilot:
            await app.refresh_active_screen()
            text = _active_text(app)
            assert "Ready for promotion" in text
            assert "Movie C.mkv" in text

    asyncio.run(run())


def test_dashboard_quick_action_opens_workflow() -> None:
    async def run() -> None:
        app = AvarchTuiApp(backend=FakeDashboardBackend(_snapshot()))
        async with app.run_test(size=(100, 30)) as pilot:
            await app.refresh_active_screen()
            await pilot.click("#dashboard-new-workflow")
            await pilot.pause()
            assert app.session_state.active_route == TuiRoute.WORKFLOW
            assert "New Workflow" in _active_text(app)

    asyncio.run(run())


def test_dashboard_empty_states_are_clear() -> None:
    async def run() -> None:
        app = AvarchTuiApp(backend=FakeDashboardBackend(_snapshot()))
        async with app.run_test(size=(100, 30)) as _pilot:
            await app.refresh_active_screen()
            text = _active_text(app)
            assert "No active jobs." in text
            assert "No failed jobs." in text
            assert "No validated jobs are waiting for promotion." in text

    asyncio.run(run())


class FakeDashboardBackend:
    def __init__(self, snapshot: DashboardSnapshot) -> None:
        self.snapshot = snapshot

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
        return self.snapshot


def _snapshot(
    *,
    totals: QueueTotals | None = None,
    active_jobs: tuple[DashboardJobSummary, ...] = (),
    recent_failures: tuple[DashboardJobSummary, ...] = (),
    promotion_ready: tuple[DashboardJobSummary, ...] = (),
) -> DashboardSnapshot:
    return DashboardSnapshot(
        revision=UiRevision(
            scheduler_generation=1,
            newest_job_updated_at=datetime(2026, 1, 1, tzinfo=UTC),
            newest_attempt_updated_at=None,
            newest_validation_created_at=None,
            newest_promotion_updated_at=None,
        ),
        scheduler=SchedulerSummary(
            mode="running",
            lease_state="active",
            runner_id="runner",
            heartbeat_at=None,
            lease_expires_at=None,
            control_generation=1,
            acknowledged_generation=1,
            cancel_pending=0,
            hold_pending=0,
        ),
        queue_totals=totals or QueueTotals(),
        active_jobs=active_jobs,
        recent_failures=recent_failures,
        promotion_ready=promotion_ready,
    )


def _job(job_id: int, file_name: str, *, status: str, stage: str) -> DashboardJobSummary:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return DashboardJobSummary(
        job_id=job_id,
        file_name=file_name,
        source_path=f"/media/{file_name}",
        status=status,
        stage=stage,
        profile_name="av1_1080p_sdr",
        priority=0,
        updated_at=now,
    )


def _active_text(app: AvarchTuiApp) -> str:
    active = app.query_one("#active-screen", RouteContent)
    return active.content_text
