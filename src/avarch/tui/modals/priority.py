from __future__ import annotations

from textual.app import ComposeResult
from textual.message import Message
from textual.widgets import Button, Input, Static


class PriorityPrompt(Static):
    class Submitted(Message):
        def __init__(self, priority: int) -> None:
            super().__init__()
            self.priority = priority

    class Canceled(Message):
        pass

    DEFAULT_CSS = """
    PriorityPrompt Button {
        margin-right: 1;
        margin-top: 1;
    }
    """

    def __init__(
        self,
        *,
        current_priority: int,
        job_count: int = 1,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.current_priority = current_priority
        self.priority = current_priority
        self.job_count = job_count
        self.error_message: str | None = None
        self.content_text = self._content_text()

    def compose(self) -> ComposeResult:
        yield Static(self.content_text, id="priority-prompt-message")
        yield Input(value=str(self.priority), id="priority-prompt-input")
        yield Button("Apply", id="priority-prompt-apply")
        yield Button("Cancel", id="priority-prompt-cancel")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "priority-prompt-input":
            return
        try:
            self.priority = int(event.value.strip())
        except ValueError:
            self.error_message = "Priority must be a whole number."
        else:
            self.error_message = None
        self.content_text = self._content_text()
        if self.is_mounted:
            self.query_one("#priority-prompt-message", Static).update(self.content_text)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "priority-prompt-apply":
            self.submit()
            event.stop()
        elif event.button.id == "priority-prompt-cancel":
            self.cancel()
            event.stop()

    def submit(self) -> None:
        if self.error_message is None:
            self.post_message(self.Submitted(self.priority))

    def cancel(self) -> None:
        self.post_message(self.Canceled())

    def _content_text(self) -> str:
        plural = "job" if self.job_count == 1 else "jobs"
        lines = [
            "Set queue priority",
            f"Current priority: {self.current_priority}",
            f"Selected: {self.job_count} {plural}",
        ]
        if self.error_message is not None:
            lines.append(f"Error: {self.error_message}")
        return "\n".join(lines)
