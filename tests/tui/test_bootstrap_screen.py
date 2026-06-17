from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from avarch.tui.app import AvarchTuiApp, RouteContent
from avarch.tui.models.bootstrap import BootstrapState, BootstrapStatus
from avarch.tui.models.common import TuiError
from avarch.tui.screens.bootstrap import BootstrapView
from avarch.tui.state import TuiRoute


def test_ready_state_opens_dashboard() -> None:
    async def run() -> None:
        app = AvarchTuiApp(backend=FakeBootstrapBackend(_status(BootstrapState.READY)))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert app.session_state.active_route == TuiRoute.DASHBOARD
            assert "Dashboard" in app.query_one("#active-screen", RouteContent).content_text

    asyncio.run(run())


def test_missing_database_shows_initialize_action() -> None:
    async def run() -> None:
        app = AvarchTuiApp(backend=FakeBootstrapBackend(_status(BootstrapState.DATABASE_MISSING)))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            view = app.query_one("#bootstrap-view", BootstrapView)
            assert "Avarch local state has not been initialized." in view.content_text
            assert str(Path("/tmp/avarch.toml")) in view.content_text
            assert app.query_one("#initialize-local-state")

    asyncio.run(run())


def test_initialize_action_uses_backend() -> None:
    async def run() -> None:
        backend = FakeBootstrapBackend(_status(BootstrapState.DATABASE_UNINITIALIZED))
        app = AvarchTuiApp(backend=backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.click("#initialize-local-state")
            await pilot.pause()
            assert backend.initialize_calls == 1
            assert app.bootstrap_status is not None
            assert app.bootstrap_status.state == BootstrapState.READY
            assert "Dashboard" in app.query_one("#active-screen", RouteContent).content_text

    asyncio.run(run())


def test_configuration_error_shows_field_path() -> None:
    status = _status(
        BootstrapState.CONFIGURATION_ERROR,
        error=TuiError(
            title="Configuration error",
            summary="logging.level: Input should be DEBUG, INFO, WARNING, ERROR or CRITICAL",
            details="/tmp/avarch.toml\nlogging.level\ninvalid value",
        ),
    )

    async def run() -> None:
        app = AvarchTuiApp(backend=FakeBootstrapBackend(status))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            view = app.query_one("#bootstrap-view", BootstrapView)
            assert "Configuration error" in view.content_text
            assert "logging.level" in view.content_text

    asyncio.run(run())


def test_profile_registry_error_shows_conflicts() -> None:
    status = _status(
        BootstrapState.PROFILE_REGISTRY_ERROR,
        error=TuiError(
            title="Profiles could not be loaded",
            summary="Duplicate profile name: film",
            details="Duplicate profile name: film (/profiles/a.toml, /profiles/b.toml)",
        ),
    )

    async def run() -> None:
        app = AvarchTuiApp(backend=FakeBootstrapBackend(status))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            text = app.query_one("#bootstrap-view", BootstrapView).content_text
            assert "Profile registry error" in text
            assert "/profiles/a.toml" in text
            assert "/profiles/b.toml" in text

    asyncio.run(run())


def test_schema_error_shows_reset_guidance() -> None:
    status = _status(
        BootstrapState.SCHEMA_ERROR,
        error=TuiError(
            title="Local state cannot be used",
            summary="This database cannot be used by this avarch build.",
            details="raw database details",
        ),
    )

    async def run() -> None:
        app = AvarchTuiApp(backend=FakeBootstrapBackend(status))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            text = app.query_one("#bootstrap-view", BootstrapView).content_text
            assert "Database schema error" in text
            assert "Move or remove the configured Avarch data directory" in text

    asyncio.run(run())


def test_bootstrap_error_does_not_show_raw_traceback() -> None:
    status = _status(
        BootstrapState.CONFIGURATION_ERROR,
        error=TuiError(
            title="Configuration error",
            summary="Invalid configuration",
            details="Traceback (most recent call last):\nsecret stack frame",
        ),
    )

    async def run() -> None:
        app = AvarchTuiApp(backend=FakeBootstrapBackend(status))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            text = app.query_one("#bootstrap-view", BootstrapView).content_text
            assert "Traceback (most recent call last):" not in text
            assert "Diagnostic details:" in text

    asyncio.run(run())


class FakeBootstrapBackend:
    def __init__(self, status: BootstrapStatus) -> None:
        self.status = status
        self.initialize_calls = 0

    async def get_bootstrap_status(self) -> BootstrapStatus:
        return self.status

    async def initialize_local_state(self) -> None:
        self.initialize_calls += 1
        self.status = replace(self.status, state=BootstrapState.READY, error=None)


def _status(
    state: BootstrapState,
    *,
    error: TuiError | None = None,
) -> BootstrapStatus:
    return BootstrapStatus(
        state=state,
        config_path=Path("/tmp/avarch.toml"),
        data_dir=Path("/tmp/.avarch"),
        database_url="sqlite:////tmp/.avarch/avarch.db",
        error=error,
    )
