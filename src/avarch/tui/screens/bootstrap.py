from __future__ import annotations

from textual.app import ComposeResult
from textual.message import Message
from textual.widgets import Button, Static

from avarch.tui.models.bootstrap import BootstrapState, BootstrapStatus


class BootstrapView(Static):
    class InitializeRequested(Message):
        pass

    DEFAULT_CSS = """
    BootstrapView {
        height: 1fr;
        padding: 1 2;
    }

    BootstrapView Button {
        margin-top: 1;
    }
    """

    def __init__(self, status: BootstrapStatus, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.status = status
        self.content_text = ""

    def compose(self) -> ComposeResult:
        yield Static(self._status_text(), id="bootstrap-message")
        if self.status.state in {
            BootstrapState.DATABASE_MISSING,
            BootstrapState.DATABASE_UNINITIALIZED,
        }:
            yield Button("Initialize local state", id="initialize-local-state")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "initialize-local-state":
            self.post_message(self.InitializeRequested())

    def _status_text(self) -> str:
        if self.status.state == BootstrapState.DATABASE_MISSING:
            text = (
                "Avarch local state has not been initialized.\n\n"
                f"Configuration:\n  {self.status.config_path}\n\n"
                f"Data directory:\n  {self.status.data_dir}"
            )
        elif self.status.state == BootstrapState.DATABASE_UNINITIALIZED:
            text = (
                "Avarch local state exists but the database has not been initialized.\n\n"
                f"Configuration:\n  {self.status.config_path}\n\n"
                f"Database:\n  {self.status.database_url}"
            )
        elif self.status.state == BootstrapState.CONFIGURATION_ERROR:
            text = _error_text(
                "Configuration error",
                self.status.error.summary if self.status.error is not None else "Invalid config.",
                self.status.error.details if self.status.error is not None else None,
            )
        elif self.status.state == BootstrapState.PROFILE_REGISTRY_ERROR:
            text = _error_text(
                "Profile registry error",
                (
                    self.status.error.summary
                    if self.status.error is not None
                    else "Profiles could not be loaded."
                ),
                self.status.error.details if self.status.error is not None else None,
            )
        elif self.status.state == BootstrapState.SCHEMA_ERROR:
            text = _error_text(
                "Database schema error",
                self.status.error.summary if self.status.error is not None else "Schema mismatch.",
                (
                    "Move or remove the configured Avarch data directory, then initialize "
                    "fresh local state."
                ),
            )
        else:
            text = "Avarch is ready."
        self.content_text = text
        return text


def _error_text(title: str, summary: str, details: str | None) -> str:
    parts = [title, "", summary]
    if details:
        parts.extend(["", _bounded_details(details)])
    return "\n".join(parts)


def _bounded_details(details: str, *, limit: int = 2000) -> str:
    scrubbed = details.replace("Traceback (most recent call last):", "Diagnostic details:")
    if len(scrubbed) <= limit:
        return scrubbed
    return f"{scrubbed[:limit]}..."
