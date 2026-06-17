from __future__ import annotations

import asyncio

from avarch.tui.app import AvarchTuiApp, RouteContent
from avarch.tui.commands import global_command_specs
from avarch.tui.state import PRIMARY_ROUTES, TuiRoute


def test_tui_boots_to_dashboard() -> None:
    async def run() -> None:
        app = AvarchTuiApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert app.session_state.active_route == TuiRoute.DASHBOARD
            assert "Dashboard" in app.query_one("#active-screen", RouteContent).content_text

    asyncio.run(run())


def test_navigation_opens_each_primary_screen() -> None:
    async def run() -> None:
        app = AvarchTuiApp()
        async with app.run_test(size=(100, 30)) as pilot:
            for route in PRIMARY_ROUTES:
                await pilot.click(f"#nav-{route.value}")
                await pilot.pause()
                assert app.session_state.active_route == route
                assert route.label in app.query_one("#active-screen", RouteContent).content_text

    asyncio.run(run())


def test_number_bindings_change_screen() -> None:
    async def run() -> None:
        app = AvarchTuiApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.press("4")
            await pilot.pause()
            assert app.session_state.active_route == TuiRoute.QUEUE
            await pilot.press("6")
            await pilot.pause()
            assert app.session_state.active_route == TuiRoute.DIAGNOSTICS

    asyncio.run(run())


def test_command_palette_exposes_navigation() -> None:
    commands = global_command_specs()

    command_routes = {command.route for command in commands if command.route is not None}
    assert command_routes == set(PRIMARY_ROUTES)
    assert "Refresh" in {command.title for command in commands}
    assert "Quit" in {command.title for command in commands}


def test_navigation_does_not_recreate_active_screen_needlessly() -> None:
    async def run() -> None:
        app = AvarchTuiApp()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            active_screen = app.query_one("#active-screen", RouteContent)
            app.open_route(TuiRoute.DASHBOARD)
            await pilot.pause()
            assert app.query_one("#active-screen", RouteContent) is active_screen

    asyncio.run(run())
