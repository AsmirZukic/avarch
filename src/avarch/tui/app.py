from __future__ import annotations

from pathlib import Path
from typing import Protocol

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.widgets import Footer, Header, Static

from avarch import __version__
from avarch.tui.backend import StaticBootstrapBackend
from avarch.tui.messages import RefreshCompleted, RefreshFailed, RefreshStarted
from avarch.tui.models.bootstrap import BootstrapState, BootstrapStatus
from avarch.tui.models.common import TuiError, UiRevision
from avarch.tui.models.dashboard import DashboardSnapshot
from avarch.tui.screens.bootstrap import BootstrapView
from avarch.tui.state import TuiRoute, TuiSessionState
from avarch.tui.widgets import StatusPanel
from avarch.tui.widgets.navigation import PrimaryNavigation


class BootstrapBackend(Protocol):
    async def get_bootstrap_status(self) -> BootstrapStatus:
        ...

    async def initialize_local_state(self) -> None:
        ...

    async def get_dashboard_snapshot(self) -> DashboardSnapshot:
        ...


class RouteContent(Static):
    content_text: str = ""

    def set_content(self, text: str) -> None:
        self.content_text = text
        self.update(text)


class AvarchTuiApp(App[None]):
    TITLE = "Avarch"
    CSS = """
    #app-shell {
        height: 1fr;
    }

    #active-screen {
        height: 1fr;
        padding: 1 2;
    }

    #status-panel {
        dock: top;
        height: auto;
        padding: 0 1;
        background: $surface;
    }
    """
    BINDINGS = [
        Binding("1", "open_dashboard", "Dashboard", show=False),
        Binding("2", "open_workflow", "New Workflow", show=False),
        Binding("3", "open_library", "Library", show=False),
        Binding("4", "open_queue", "Queue", show=False),
        Binding("5", "open_profiles", "Profiles", show=False),
        Binding("6", "open_diagnostics", "Diagnostics", show=False),
        Binding("r", "refresh", "Refresh"),
        Binding("q", "request_quit", "Quit"),
    ]

    def __init__(
        self,
        *,
        backend: BootstrapBackend | None = None,
        initialized: bool = False,
        database_url: str = "unknown",
        exit_after_mount: bool = False,
    ) -> None:
        super().__init__()
        self.initialized = initialized
        self.database_url = database_url
        self.exit_after_mount = exit_after_mount
        self.session_state = TuiSessionState()
        self.backend = backend or StaticBootstrapBackend(
            BootstrapStatus(
                state=BootstrapState.READY,
                config_path=Path("avarch.toml"),
                data_dir=Path(".avarch"),
                database_url=database_url,
            )
        )
        self.bootstrap_status: BootstrapStatus | None = None
        self.refresh_events: list[RefreshStarted | RefreshCompleted | RefreshFailed] = []
        self.last_refresh_error: TuiError | None = None
        self.snapshot_render_count = 0
        self._latest_refresh_request = 0
        self._refresh_running = False
        self._refresh_pending = False
        self._last_revision_by_route: dict[TuiRoute, UiRevision] = {}

    def compose(self) -> ComposeResult:
        status = "initialized" if self.initialized else "not initialized"
        yield Header()
        yield StatusPanel(
            f"Status: {status}   Database: {self.database_url}   Version: {__version__}",
            id="status-panel",
        )
        with Horizontal(id="app-shell"):
            yield PrimaryNavigation()
            with Container(id="screen-container"):
                yield RouteContent("", id="active-screen")
        yield Footer()

    async def on_mount(self) -> None:
        await self.bootstrap()
        if self.exit_after_mount:
            self.exit()

    async def bootstrap(self) -> None:
        status = await self.backend.get_bootstrap_status()
        self.bootstrap_status = status
        if status.state == BootstrapState.READY:
            await self._restore_route_content()
            self.open_route(TuiRoute.DASHBOARD)
            return
        await self._render_bootstrap(status)

    async def on_bootstrap_view_initialize_requested(
        self,
        _event: BootstrapView.InitializeRequested,
    ) -> None:
        await self.backend.initialize_local_state()
        await self.bootstrap()

    def on_primary_navigation_route_selected(
        self,
        event: PrimaryNavigation.RouteSelected,
    ) -> None:
        self.open_route(event.route)

    def open_route(self, route: TuiRoute) -> None:
        if (
            self.bootstrap_status is not None
            and self.bootstrap_status.state != BootstrapState.READY
        ):
            return
        if route == self.session_state.active_route:
            self._render_active_route()
            return
        self.session_state.active_route = route
        self._render_active_route()

    def action_open_dashboard(self) -> None:
        self.open_route(TuiRoute.DASHBOARD)

    def action_open_workflow(self) -> None:
        self.open_route(TuiRoute.WORKFLOW)

    def action_open_library(self) -> None:
        self.open_route(TuiRoute.LIBRARY)

    def action_open_queue(self) -> None:
        self.open_route(TuiRoute.QUEUE)

    def action_open_profiles(self) -> None:
        self.open_route(TuiRoute.PROFILES)

    def action_open_diagnostics(self) -> None:
        self.open_route(TuiRoute.DIAGNOSTICS)

    async def action_refresh(self) -> None:
        await self.refresh_active_screen()

    def action_request_quit(self) -> None:
        self.exit()

    def _render_active_route(self) -> None:
        route = self.session_state.active_route
        active = self.query_one("#active-screen", RouteContent)
        active.set_content(f"{route.label}\n\n{_route_empty_state(route)}")

    async def refresh_active_screen(self) -> None:
        route = self.session_state.active_route
        self._latest_refresh_request += 1
        if self._refresh_running:
            self._refresh_pending = True
            return

        self._refresh_running = True
        try:
            while True:
                request_id = self._latest_refresh_request
                self._refresh_pending = False
                self.refresh_events.append(RefreshStarted(route=route, request_id=request_id))
                try:
                    snapshot = await self._load_snapshot(route)
                except Exception as exc:
                    error = TuiError(
                        title="Refresh failed",
                        summary=str(exc) or exc.__class__.__name__,
                        details=None,
                    )
                    self.last_refresh_error = error
                    self.refresh_events.append(
                        RefreshFailed(route=route, request_id=request_id, error=error)
                    )
                    break

                if request_id == self._latest_refresh_request:
                    rendered = self._render_snapshot_if_changed(route, snapshot)
                    self.refresh_events.append(
                        RefreshCompleted(
                            route=route,
                            request_id=request_id,
                            revision=snapshot.revision,
                            rendered=rendered,
                        )
                    )
                if not self._refresh_pending:
                    break
        finally:
            self._refresh_running = False

    async def _load_snapshot(self, route: TuiRoute) -> DashboardSnapshot:
        if route == TuiRoute.DASHBOARD:
            return await self.backend.get_dashboard_snapshot()
        return await self.backend.get_dashboard_snapshot()

    def _render_snapshot_if_changed(
        self,
        route: TuiRoute,
        snapshot: DashboardSnapshot,
    ) -> bool:
        if self._last_revision_by_route.get(route) == snapshot.revision:
            return False
        self._last_revision_by_route[route] = snapshot.revision
        active = self.query_one("#active-screen", RouteContent)
        active.set_content(_dashboard_snapshot_text(snapshot))
        self.snapshot_render_count += 1
        self.last_refresh_error = None
        return True

    async def _render_bootstrap(self, status: BootstrapStatus) -> None:
        container = self.query_one("#screen-container", Container)
        container.remove_children()
        await container.mount(BootstrapView(status, id="bootstrap-view"))

    async def _restore_route_content(self) -> None:
        if len(self.query("#active-screen")) > 0:
            return
        container = self.query_one("#screen-container", Container)
        container.remove_children()
        await container.mount(RouteContent("", id="active-screen"))


def _route_empty_state(route: TuiRoute) -> str:
    return {
        TuiRoute.DASHBOARD: (
            "Scheduler, queue, failures, and promotion-ready work will appear here."
        ),
        TuiRoute.WORKFLOW: "Select folders, scan media, review candidates, and enqueue workflows.",
        TuiRoute.LIBRARY: "Persistent media inventory will appear here.",
        TuiRoute.QUEUE: "Queued workflows and scheduler controls will appear here.",
        TuiRoute.PROFILES: "Built-in and user profiles will appear here.",
        TuiRoute.DIAGNOSTICS: "Configuration, database, and tool health will appear here.",
    }[route]


def _dashboard_snapshot_text(snapshot: DashboardSnapshot) -> str:
    totals = snapshot.queue_totals
    return (
        "Dashboard\n\n"
        f"Scheduler: {snapshot.scheduler.mode.upper()}\n"
        f"Lease: {snapshot.scheduler.lease_state}\n"
        f"Pending: {totals.pending}\n"
        f"Running: {totals.running}\n"
        f"Failed: {totals.failed}\n"
        f"Validated: {totals.validated}\n"
        f"Completed: {totals.completed}"
    )
