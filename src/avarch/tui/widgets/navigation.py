from __future__ import annotations

from textual.app import ComposeResult
from textual.message import Message
from textual.widgets import Button, Static

from avarch.tui.state import PRIMARY_ROUTES, TuiRoute


class PrimaryNavigation(Static):
    class RouteSelected(Message):
        def __init__(self, route: TuiRoute) -> None:
            super().__init__()
            self.route = route

    DEFAULT_CSS = """
    PrimaryNavigation {
        width: 18;
        min-width: 18;
        border-right: solid $surface;
        padding: 1;
    }

    PrimaryNavigation Button {
        width: 100%;
        margin-bottom: 1;
    }
    """

    def compose(self) -> ComposeResult:
        for route in PRIMARY_ROUTES:
            yield Button(route.label, id=f"nav-{route.value}", classes="nav-button")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        route_name = event.button.id.removeprefix("nav-") if event.button.id is not None else ""
        try:
            route = TuiRoute(route_name)
        except ValueError:
            return
        self.post_message(self.RouteSelected(route))
