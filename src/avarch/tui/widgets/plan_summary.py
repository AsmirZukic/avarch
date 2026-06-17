from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.widgets import Static

from avarch.tui.models.workflow import WorkflowPreview, WorkflowPreviewRow


class PlanSummary(Static):
    def __init__(self, preview: WorkflowPreview, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.preview = preview
        self.content_text = ""

    def compose(self) -> ComposeResult:
        yield Static("", id="plan-summary-content")

    async def on_mount(self) -> None:
        self.render_summary()

    def update_preview(self, preview: WorkflowPreview) -> None:
        self.preview = preview
        self.render_summary()

    def render_summary(self) -> None:
        text = plan_summary_text(self.preview)
        self.content_text = text
        if self.is_mounted:
            self.query_one("#plan-summary-content", Static).update(text)


def plan_summary_text(preview: WorkflowPreview) -> str:
    lines = [
        "Workflow preview",
        "",
        preview.summary,
        "",
        (
            "FILE                 RESULT          VIDEO                    "
            "AUDIO                    SUBTITLES"
        ),
    ]
    if not preview.rows:
        lines.append("No preview rows are available.")
        return "\n".join(lines)
    for row in preview.rows:
        lines.append(_row_text(row))
        if row.reason:
            lines.append(f"  {row.reason}")
    return "\n".join(lines)


def _row_text(row: WorkflowPreviewRow) -> str:
    return (
        f"{Path(row.path).name:<20} "
        f"{row.result:<15} "
        f"{_clip(row.video, 24):<24} "
        f"{_clip(row.audio, 24):<24} "
        f"{_clip(row.subtitles, 24)}"
    )


def _clip(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."
