from __future__ import annotations

from textual.app import App, ComposeResult
from textual.widgets import Footer, Header, Static, TabbedContent, TabPane

from avarch import __version__
from avarch.tui.widgets import StatusPanel


class AvarchTuiApp(App[None]):
    TITLE = "Avarch"

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

    def compose(self) -> ComposeResult:
        status = "initialized" if self.initialized else "not initialized"
        yield Header()
        yield StatusPanel(
            f"Avarch\nStatus: {status}\nDatabase: {self.database_url}\nVersion: {__version__}"
        )
        with TabbedContent(initial="dashboard"):
            with TabPane("Dashboard", id="dashboard"):
                yield Static("Dashboard")
            with TabPane("Jobs", id="jobs"):
                yield Static("Jobs")
            with TabPane("Profiles", id="profiles"):
                yield Static("Profiles")
            with TabPane("Logs", id="logs"):
                yield Static("Logs")
        yield Footer()

    def on_mount(self) -> None:
        if self.exit_after_mount:
            self.exit()
