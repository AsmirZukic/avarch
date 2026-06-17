from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.message import Message
from textual.widgets import Button, Static

from avarch.tui.models.dashboard import DashboardJobSummary, DashboardSnapshot
from avarch.tui.state import TuiRoute
from avarch.tui.widgets.scheduler_panel import scheduler_summary_text


class DashboardView(Static):
    class QuickActionSelected(Message):
        def __init__(self, route: TuiRoute) -> None:
            super().__init__()
            self.route = route

    DEFAULT_CSS = """
    DashboardView {
        height: 1fr;
        padding: 1 2;
    }

    DashboardView Button {
        margin-right: 1;
        margin-top: 1;
    }
    """

    def __init__(self, snapshot: DashboardSnapshot, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.snapshot = snapshot
        self.content_text = dashboard_snapshot_text(snapshot)

    def compose(self) -> ComposeResult:
        yield Static(self.content_text, id="dashboard-summary")
        yield Button("New Workflow", id="dashboard-new-workflow")
        yield Button("Open Queue", id="dashboard-open-queue")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "dashboard-new-workflow":
            self.post_message(self.QuickActionSelected(TuiRoute.WORKFLOW))
        elif event.button.id == "dashboard-open-queue":
            self.post_message(self.QuickActionSelected(TuiRoute.QUEUE))


def dashboard_snapshot_text(snapshot: DashboardSnapshot) -> str:
    return "\n\n".join(
        (
            scheduler_summary_text(snapshot.scheduler),
            _queue_text(snapshot),
            _jobs_text("Active work", snapshot.active_jobs, empty="No active jobs."),
            _jobs_text("Recent failures", snapshot.recent_failures, empty="No failed jobs."),
            _jobs_text(
                "Ready for promotion",
                snapshot.promotion_ready,
                empty="No validated jobs are waiting for promotion.",
            ),
            "Quick actions\n  New Workflow\n  Open Queue",
        )
    )


def _queue_text(snapshot: DashboardSnapshot) -> str:
    totals = snapshot.queue_totals
    return (
        "Queue\n"
        f"  Pending: {totals.pending}\n"
        f"  Running: {totals.running}\n"
        f"  Held: {totals.held}\n"
        f"  Failed: {totals.failed}\n"
        f"  Validated: {totals.validated}\n"
        f"  Canceled: {totals.canceled}\n"
        f"  Completed: {totals.completed}"
    )


def _jobs_text(
    title: str,
    jobs: tuple[DashboardJobSummary, ...],
    *,
    empty: str,
) -> str:
    if not jobs:
        return f"{title}\n  {empty}"
    lines = [title]
    for job in jobs:
        file_name = Path(job.source_path).name
        lines.append(
            f"  {file_name}  {job.status.upper()} {job.stage}  profile: {job.profile_name}"
        )
    return "\n".join(lines)
