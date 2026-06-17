from __future__ import annotations

from typing import Protocol

from textual.app import ComposeResult
from textual.message import Message
from textual.widgets import Button, Static

from avarch.tui.backend import SchedulerControlRequest, SchedulerControlResult
from avarch.tui.models.dashboard import SchedulerSummary
from avarch.tui.models.queue import QueueFilters, QueueSnapshot
from avarch.tui.widgets.queue_table import QueueTable
from avarch.tui.widgets.scheduler_panel import SchedulerPanel


class QueueBackend(Protocol):
    async def get_queue_snapshot(self, filters: QueueFilters) -> QueueSnapshot:
        ...

    async def request_scheduler_control(
        self,
        request: SchedulerControlRequest,
    ) -> SchedulerControlResult:
        ...


class QueueView(Static):
    class JobOpenRequested(Message):
        def __init__(self, job_id: int) -> None:
            super().__init__()
            self.job_id = job_id

    DEFAULT_CSS = """
    QueueView {
        height: 1fr;
        padding: 1;
    }

    QueueView Button {
        margin-right: 1;
        margin-top: 1;
    }
    """

    def __init__(
        self,
        *,
        backend: QueueBackend,
        snapshot: QueueSnapshot,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.backend = backend
        self.snapshot = snapshot
        self.filters = snapshot.filters
        self.scheduler_panel = SchedulerPanel(
            snapshot.scheduler,
            backend=backend,
            refresh_status=self.refresh_scheduler_status,
            id="queue-scheduler-panel",
        )
        self.table = QueueTable(snapshot, id="queue-table")
        self.phase = "Queue"
        self.error_message: str | None = None
        self.content_text = ""

    def compose(self) -> ComposeResult:
        yield Static("", id="queue-status")
        yield self.scheduler_panel
        yield self.table
        yield Button("Open selected job", id="queue-open-selected")

    async def on_mount(self) -> None:
        self._render_status()

    async def apply_filters(self, filters: QueueFilters) -> None:
        self.filters = filters
        await self.refresh_queue()

    async def refresh_queue(self) -> None:
        self.phase = "Loading queue"
        self.error_message = None
        self._render_status()
        try:
            self.snapshot = await self.backend.get_queue_snapshot(self.filters)
        except Exception as exc:
            self.phase = "Queue load failed"
            self.error_message = str(exc) or exc.__class__.__name__
            self._render_status()
            return
        self.phase = "Queue"
        self.scheduler_panel.update_scheduler(self.snapshot.scheduler)
        self.table.update_snapshot(self.snapshot)
        self._render_status()

    async def refresh_scheduler_status(self) -> SchedulerSummary:
        self.snapshot = await self.backend.get_queue_snapshot(self.filters)
        self.table.update_snapshot(self.snapshot)
        self._render_status()
        return self.snapshot.scheduler

    def on_queue_table_selection_changed(
        self,
        _event: QueueTable.SelectionChanged,
    ) -> None:
        self._render_status()

    def on_queue_table_job_opened(self, event: QueueTable.JobOpened) -> None:
        self.post_message(self.JobOpenRequested(event.job_id))
        event.stop()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "queue-open-selected":
            self.open_selected_job()
            event.stop()

    def open_selected_job(self) -> None:
        job_id = self.table.first_selected_job_id()
        if job_id is None:
            self.phase = "Selection required"
            self.error_message = "Select a queued job before opening details."
            self._render_status()
            return
        self.post_message(self.JobOpenRequested(job_id))

    def _render_status(self) -> None:
        status = self._status_text()
        self.content_text = "\n\n".join([status, self.table.content_text])
        if self.is_mounted:
            self.query_one("#queue-status", Static).update(status)

    def _status_text(self) -> str:
        lines = [
            "Queue",
            "",
            f"Phase: {self.phase}",
            f"Visible jobs: {len(self.snapshot.rows)}",
            f"Selected jobs: {len(self.table.selected_job_ids)}",
            f"Scheduler: {self.snapshot.scheduler.mode}",
        ]
        filter_text = _filters_text(self.filters)
        if filter_text:
            lines.extend(["", filter_text])
        if self.error_message is not None:
            lines.extend(["", f"Error: {self.error_message}"])
        return "\n".join(lines)


def _filters_text(filters: QueueFilters) -> str:
    parts: list[str] = []
    if filters.statuses is not None:
        parts.append("status=" + ",".join(sorted(status.value for status in filters.statuses)))
    if filters.stages is not None:
        parts.append("stage=" + ",".join(sorted(stage.value for stage in filters.stages)))
    if filters.profile_name is not None:
        parts.append(f"profile={filters.profile_name}")
    if filters.search_text:
        parts.append(f"search={filters.search_text}")
    if filters.active_only:
        parts.append("active only")
    if filters.failures_only:
        parts.append("failures only")
    if filters.promotion_ready_only:
        parts.append("promotion ready")
    return "Filters: " + "; ".join(parts) if parts else ""
