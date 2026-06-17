from __future__ import annotations

from textual.app import ComposeResult
from textual.message import Message
from textual.widgets import Button, Input, Static


class ReasonPrompt(Static):
    class Submitted(Message):
        def __init__(self, *, action: str, reason: str | None) -> None:
            super().__init__()
            self.action = action
            self.reason = reason

    class Canceled(Message):
        pass

    DEFAULT_CSS = """
    ReasonPrompt Button {
        margin-right: 1;
        margin-top: 1;
    }
    """

    def __init__(
        self,
        *,
        action: str,
        job_count: int,
        message: str,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.action = action
        self.job_count = job_count
        self.message = message
        self.reason: str | None = None
        self.content_text = self._content_text()

    def compose(self) -> ComposeResult:
        yield Static(self.content_text, id="reason-prompt-message")
        yield Input(placeholder="Reason (optional)", id="reason-prompt-input")
        yield Button("Apply", id="reason-prompt-apply")
        yield Button("Cancel", id="reason-prompt-cancel")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "reason-prompt-input":
            value = event.value.strip()
            self.reason = value or None
            self.content_text = self._content_text()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "reason-prompt-apply":
            self.submit()
            event.stop()
        elif event.button.id == "reason-prompt-cancel":
            self.cancel()
            event.stop()

    def submit(self) -> None:
        self.post_message(self.Submitted(action=self.action, reason=self.reason))

    def cancel(self) -> None:
        self.post_message(self.Canceled())

    def _content_text(self) -> str:
        plural = "job" if self.job_count == 1 else "jobs"
        lines = [
            self.message,
            f"Action: {self.action}",
            f"Selected: {self.job_count} {plural}",
        ]
        if self.reason:
            lines.append(f"Reason: {self.reason}")
        return "\n".join(lines)
