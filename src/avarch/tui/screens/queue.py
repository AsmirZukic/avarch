from __future__ import annotations

from typing import Protocol

from textual.app import ComposeResult
from textual.message import Message
from textual.widgets import Button, Static

from avarch.tui.backend import (
    JobActionRequest,
    JobActionResult,
    SchedulerControlRequest,
    SchedulerControlResult,
)
from avarch.tui.modals.priority import PriorityPrompt
from avarch.tui.modals.reason import ReasonPrompt
from avarch.tui.models.dashboard import SchedulerSummary
from avarch.tui.models.queue import QueueFilters, QueueJobRow, QueueSnapshot
from avarch.tui.widgets.queue_table import QueueTable
from avarch.tui.widgets.scheduler_panel import SchedulerPanel

JOB_ACTION_LABELS = {
    "hold": "Hold",
    "release": "Release",
    "cancel": "Cancel",
    "retry": "Retry",
    "priority": "Priority",
}


class QueueBackend(Protocol):
    async def get_queue_snapshot(self, filters: QueueFilters) -> QueueSnapshot:
        ...

    async def request_scheduler_control(
        self,
        request: SchedulerControlRequest,
    ) -> SchedulerControlResult:
        ...

    async def perform_job_action(self, request: JobActionRequest) -> JobActionResult:
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
        self.action_prompt: ReasonPrompt | PriorityPrompt | None = None
        self.content_text = ""

    def compose(self) -> ComposeResult:
        yield Static("", id="queue-status")
        yield self.scheduler_panel
        yield self.table
        yield Button("Open selected job", id="queue-open-selected")
        yield Button("Hold", id="queue-hold-selected")
        yield Button("Release", id="queue-release-selected")
        yield Button("Cancel", id="queue-cancel-selected")
        yield Button("Retry", id="queue-retry-selected")
        yield Button("Priority", id="queue-priority-selected")

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

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "queue-open-selected":
            self.open_selected_job()
            event.stop()
        elif event.button.id == "queue-hold-selected":
            self.prepare_job_action("hold")
            event.stop()
        elif event.button.id == "queue-cancel-selected":
            self.prepare_job_action("cancel")
            event.stop()
        elif event.button.id == "queue-priority-selected":
            self.prepare_job_action("priority")
            event.stop()
        elif event.button.id == "queue-release-selected":
            await self.perform_selected_job_action("release")
            event.stop()
        elif event.button.id == "queue-retry-selected":
            await self.perform_selected_job_action("retry")
            event.stop()

    async def on_reason_prompt_submitted(self, event: ReasonPrompt.Submitted) -> None:
        await self.perform_selected_job_action(event.action, reason=event.reason)
        event.stop()

    async def on_priority_prompt_submitted(self, event: PriorityPrompt.Submitted) -> None:
        await self.perform_selected_job_action("priority", priority=event.priority)
        event.stop()

    def open_selected_job(self) -> None:
        job_id = self.table.first_selected_job_id()
        if job_id is None:
            self.phase = "Selection required"
            self.error_message = "Select a queued job before opening details."
            self._render_status()
            return
        self.post_message(self.JobOpenRequested(job_id))

    def selected_job_row(self) -> QueueJobRow | None:
        job_id = self.table.first_selected_job_id()
        if job_id is None:
            return None
        return self._row_by_id(job_id)

    def selected_job_ids(self) -> tuple[int, ...]:
        return tuple(sorted(self.table.selected_job_ids))

    def available_actions_for_selected(self) -> tuple[str, ...]:
        row = self.selected_job_row()
        if row is None:
            return ()
        return available_job_actions(row)

    def prepare_job_action(self, action: str) -> ReasonPrompt | PriorityPrompt | None:
        selected_ids = self.selected_job_ids()
        if not selected_ids:
            self.phase = "Selection required"
            self.error_message = "Select one or more queued jobs before choosing an action."
            self.action_prompt = None
            self._render_status()
            return None
        if not self._selection_supports_action(action):
            self.phase = "Action unavailable"
            self.error_message = unavailable_action_message(action)
            self.action_prompt = None
            self._render_status()
            return None

        if action == "priority":
            row = self.selected_job_row()
            prompt: ReasonPrompt | PriorityPrompt = PriorityPrompt(
                current_priority=row.priority if row is not None else 0,
                job_count=len(selected_ids),
            )
        else:
            prompt = ReasonPrompt(
                action=action,
                job_count=len(selected_ids),
                message=job_action_help(self.selected_job_row(), action),
            )
        self.phase = f"Confirm {JOB_ACTION_LABELS.get(action, action).lower()}"
        self.error_message = None
        self.action_prompt = prompt
        self._render_status()
        return prompt

    async def perform_selected_job_action(
        self,
        action: str,
        *,
        reason: str | None = None,
        priority: int | None = None,
    ) -> JobActionResult | None:
        selected_ids = self.selected_job_ids()
        if not selected_ids:
            self.phase = "Selection required"
            self.error_message = "Select one or more queued jobs before choosing an action."
            self._render_status()
            return None
        if not self._selection_supports_action(action):
            self.phase = "Action unavailable"
            self.error_message = unavailable_action_message(action)
            self._render_status()
            return None
        if action == "priority" and priority is None:
            self.phase = "Priority required"
            self.error_message = "Enter a new priority before applying the action."
            self._render_status()
            return None

        request = JobActionRequest(
            job_ids=selected_ids,
            action=action,
            reason=reason,
            priority=priority,
        )
        try:
            result = await self.backend.perform_job_action(request)
        except Exception as exc:
            await self._refresh_after_action_failure(str(exc) or exc.__class__.__name__)
            return None

        await self._reload_queue_snapshot()
        self.phase = result.message
        self.error_message = None
        self.action_prompt = None
        self._render_status()
        return result

    def _render_status(self) -> None:
        status = self._status_text()
        self.content_text = "\n\n".join([status, self.table.content_text])
        if self.is_mounted:
            self.query_one("#queue-status", Static).update(status)

    async def _reload_queue_snapshot(self) -> None:
        self.snapshot = await self.backend.get_queue_snapshot(self.filters)
        self.scheduler_panel.update_scheduler(self.snapshot.scheduler)
        self.table.update_snapshot(self.snapshot)

    async def _refresh_after_action_failure(self, message: str) -> None:
        try:
            await self._reload_queue_snapshot()
        finally:
            self.phase = "Job action failed"
            self.error_message = message
            self.action_prompt = None
            self._render_status()

    def _selection_supports_action(self, action: str) -> bool:
        selected_ids = set(self.selected_job_ids())
        rows = [row for row in self.snapshot.rows if row.job_id in selected_ids]
        return bool(rows) and all(action in available_job_actions(row) for row in rows)

    def _row_by_id(self, job_id: int) -> QueueJobRow | None:
        for row in self.snapshot.rows:
            if row.job_id == job_id:
                return row
        return None

    def _status_text(self) -> str:
        actions = available_job_actions_text(self.selected_job_row())
        lines = [
            "Queue",
            "",
            f"Phase: {self.phase}",
            f"Visible jobs: {len(self.snapshot.rows)}",
            f"Selected jobs: {len(self.table.selected_job_ids)}",
            f"Scheduler: {self.snapshot.scheduler.mode}",
            f"Available actions: {actions}",
        ]
        help_text = selected_action_help(self.selected_job_row())
        if help_text:
            lines.extend(["", help_text])
        if self.action_prompt is not None:
            lines.extend(["", self.action_prompt.content_text])
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


def available_job_actions(row: QueueJobRow) -> tuple[str, ...]:
    status = row.status.lower()
    stage = row.stage.lower()
    if status == "pending":
        return ("hold", "cancel", "priority")
    if status == "held":
        return ("release", "cancel", "priority")
    if status in {"failed", "canceled"}:
        return ("retry", "cancel")
    if status == "running" and stage != "promote":
        return ("hold", "cancel")
    return ()


def available_job_actions_text(row: QueueJobRow | None) -> str:
    if row is None:
        return "select a job"
    actions = available_job_actions(row)
    if not actions:
        return "none"
    return ", ".join(JOB_ACTION_LABELS[action] for action in actions)


def selected_action_help(row: QueueJobRow | None) -> str:
    if row is None:
        return ""
    if row.status.lower() == "running" and row.stage.lower() != "promote":
        return job_action_help(row, "cancel")
    if row.status.lower() == "running" and row.stage.lower() == "promote":
        return (
            "Active promotion jobs are controlled by promotion recovery, "
            "not scheduler cancellation."
        )
    return ""


def job_action_help(row: QueueJobRow | None, action: str) -> str:
    if action == "cancel" and row is not None and row.status.lower() == "running":
        return (
            "Cancel requests the running worker to stop; the job changes state "
            "after the scheduler observes it."
        )
    if action == "hold" and row is not None and row.status.lower() == "running":
        return "Hold requests the running worker to pause before the next scheduler stage."
    return f"Confirm {JOB_ACTION_LABELS.get(action, action).lower()} for the selected job."


def unavailable_action_message(action: str) -> str:
    return f"{JOB_ACTION_LABELS.get(action, action)} is not available for the selection."
