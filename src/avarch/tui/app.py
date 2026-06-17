from __future__ import annotations

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.widgets import Footer, Header, Static

from avarch import __version__
from avarch.tui.state import TuiRoute, TuiSessionState
from avarch.tui.widgets import StatusPanel
from avarch.tui.widgets.navigation import PrimaryNavigation


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
        initialized: bool = False,
        database_url: str = "unknown",
        exit_after_mount: bool = False,
    ) -> None:
        super().__init__()
        self.initialized = initialized
        self.database_url = database_url
        self.exit_after_mount = exit_after_mount
        self.session_state = TuiSessionState()

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

    def on_mount(self) -> None:
        self.open_route(TuiRoute.DASHBOARD)
        if self.exit_after_mount:
            self.exit()

    def on_primary_navigation_route_selected(
        self,
        event: PrimaryNavigation.RouteSelected,
    ) -> None:
        self.open_route(event.route)

    def open_route(self, route: TuiRoute) -> None:
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

    def action_refresh(self) -> None:
        self._render_active_route()

    def action_request_quit(self) -> None:
        self.exit()

    def _render_active_route(self) -> None:
        route = self.session_state.active_route
        active = self.query_one("#active-screen", RouteContent)
        active.set_content(f"{route.label}\n\n{_route_empty_state(route)}")


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
