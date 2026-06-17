from __future__ import annotations

from textual.app import ComposeResult
from textual.message import Message
from textual.widgets import Button, Static

from avarch.tui.models.queue import QueueClearPreview, QueueRetryPreview


class QueueBulkActionPrompt(Static):
    class Confirmed(Message):
        def __init__(self, confirmed: bool = False) -> None:
            super().__init__()
            self.confirmed = confirmed

    class Canceled(Message):
        pass

    DEFAULT_CSS = """
    QueueBulkActionPrompt Button {
        margin-right: 1;
        margin-top: 1;
    }
    """

    def __init__(
        self,
        *,
        action: str,
        preview: QueueClearPreview | QueueRetryPreview,
        second_confirmation_required: bool = False,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.action = action
        self.preview = preview
        self.second_confirmation_required = second_confirmation_required
        self.content_text = queue_bulk_preview_text(
            action=action,
            preview=preview,
            second_confirmation_required=second_confirmation_required,
        )

    def compose(self) -> ComposeResult:
        yield Static(self.content_text, id="queue-bulk-action-message")
        yield Button("Confirm", id="queue-bulk-action-confirm")
        yield Button("Cancel", id="queue-bulk-action-cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "queue-bulk-action-confirm":
            self.confirm()
            event.stop()
        elif event.button.id == "queue-bulk-action-cancel":
            self.cancel()
            event.stop()

    def require_second_confirmation(self) -> None:
        self.second_confirmation_required = True
        self.content_text = queue_bulk_preview_text(
            action=self.action,
            preview=self.preview,
            second_confirmation_required=True,
        )
        if self.is_mounted:
            self.query_one("#queue-bulk-action-message", Static).update(self.content_text)

    def confirm(self) -> None:
        self.post_message(self.Confirmed(confirmed=self.second_confirmation_required))

    def cancel(self) -> None:
        self.post_message(self.Canceled())


def queue_bulk_preview_text(
    *,
    action: str,
    preview: QueueClearPreview | QueueRetryPreview,
    second_confirmation_required: bool = False,
) -> str:
    if action == "cancel" and isinstance(preview, QueueClearPreview):
        lines = [
            "Bulk cancel preview",
            f"Matched: {preview.matched}",
            f"Cancel immediately: {preview.cancel_immediately}",
            f"Request running interruption: {preview.request_interruption}",
            f"Active promotions excluded: {preview.active_promotions_excluded}",
            f"Completed excluded: {preview.completed_excluded}",
        ]
    elif action == "retry" and isinstance(preview, QueueRetryPreview):
        lines = [
            "Bulk retry preview",
            f"Matched: {preview.matched}",
            f"Retryable: {preview.retryable}",
            f"Resume at probe: {preview.reset_to_probe}",
            f"Resume at plan: {preview.reset_to_plan}",
            f"Resume at encode: {preview.reset_to_encode}",
            f"Resume at validate: {preview.reset_to_validate}",
            f"Resume at promote: {preview.return_to_promote}",
            f"Requires requeue: {preview.requires_requeue}",
        ]
    else:
        lines = ["Bulk action preview", f"Matched: {preview.matched}"]
    if second_confirmation_required:
        lines.extend(["", "Confirm again to apply this change."])
    return "\n".join(lines)
