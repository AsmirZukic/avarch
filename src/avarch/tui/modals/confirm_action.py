from __future__ import annotations

from textual.app import ComposeResult
from textual.message import Message
from textual.widgets import Button, Static


class ConfirmAction(Static):
    class Confirmed(Message):
        pass

    class Canceled(Message):
        pass

    DEFAULT_CSS = """
    ConfirmAction Button {
        margin-right: 1;
        margin-top: 1;
    }
    """

    def __init__(self, message: str, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.message = message
        self.content_text = message

    def compose(self) -> ComposeResult:
        yield Static(self.message, id="confirm-action-message")
        yield Button("Confirm", id="confirm-action-confirm")
        yield Button("Cancel", id="confirm-action-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-action-confirm":
            self.confirm()
            event.stop()
        elif event.button.id == "confirm-action-cancel":
            self.cancel()
            event.stop()

    def confirm(self) -> None:
        self.post_message(self.Confirmed())

    def cancel(self) -> None:
        self.post_message(self.Canceled())
