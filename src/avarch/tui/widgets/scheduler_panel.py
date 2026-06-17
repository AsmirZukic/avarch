from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from textual.app import ComposeResult
from textual.message import Message
from textual.widgets import Button, Static

from avarch.tui.backend import SchedulerControlRequest, SchedulerControlResult
from avarch.tui.models.dashboard import SchedulerSummary


class SchedulerControlBackend(Protocol):
    async def request_scheduler_control(
        self,
        request: SchedulerControlRequest,
    ) -> SchedulerControlResult:
        ...


class SchedulerPanel(Static):
    class ControlCompleted(Message):
        def __init__(self, action: str, result: SchedulerControlResult) -> None:
            super().__init__()
            self.action = action
            self.result = result

    DEFAULT_CSS = """
    SchedulerPanel Button {
        margin-right: 1;
        margin-top: 1;
    }
    """

    def __init__(
        self,
        scheduler: SchedulerSummary,
        *,
        backend: SchedulerControlBackend,
        refresh_status: Callable[[], Awaitable[SchedulerSummary]] | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.scheduler = scheduler
        self.backend = backend
        self.refresh_status = refresh_status
        self.pending_action: str | None = None
        self.confirmation_text = ""
        self.error_message: str | None = None
        self.content_text = ""

    def compose(self) -> ComposeResult:
        yield Static("", id="scheduler-panel-content")
        yield Button("Start Scheduler", id="scheduler-start")
        yield Button("Pause", id="scheduler-pause")
        yield Button("Resume", id="scheduler-resume")
        yield Button("Drain", id="scheduler-drain")
        yield Button("Stop", id="scheduler-stop")

    async def on_mount(self) -> None:
        self.render_panel()

    def update_scheduler(self, scheduler: SchedulerSummary) -> None:
        self.scheduler = scheduler
        self.render_panel()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "scheduler-start":
            self.prepare_action("start")
            event.stop()
        elif button_id == "scheduler-pause":
            self.prepare_action("pause")
            event.stop()
        elif button_id == "scheduler-resume":
            self.run_worker(self.confirm_action("resume"), exclusive=True)
            event.stop()
        elif button_id == "scheduler-drain":
            self.prepare_action("drain")
            event.stop()
        elif button_id == "scheduler-stop":
            self.prepare_action("stop")
            event.stop()

    def prepare_action(self, action: str) -> None:
        self.pending_action = action
        self.error_message = None
        self.confirmation_text = scheduler_confirmation_text(action)
        self.render_panel()

    async def confirm_pending_action(self) -> None:
        if self.pending_action is None:
            return
        await self.confirm_action(self.pending_action)

    async def confirm_action(self, action: str) -> None:
        if action == "start":
            self.error_message = "Starting the in-process scheduler is handled separately."
            self.render_panel()
            return
        try:
            result = await self.backend.request_scheduler_control(
                SchedulerControlRequest(action=action, reason="tui")
            )
        except Exception as exc:
            self.error_message = str(exc) or exc.__class__.__name__
            if self.refresh_status is not None:
                self.scheduler = await self.refresh_status()
            self.render_panel()
            return
        self.pending_action = None
        self.confirmation_text = ""
        self.error_message = None
        if self.refresh_status is not None:
            self.scheduler = await self.refresh_status()
        self.render_panel()
        self.post_message(self.ControlCompleted(action, result))

    def available_actions(self) -> tuple[str, ...]:
        mode = self.scheduler.mode.lower()
        if mode == "running":
            return ("pause", "drain", "stop")
        if mode == "paused":
            return ("resume", "stop")
        if mode == "draining":
            return ("draining",)
        if mode == "stopping":
            return ("stopping",)
        return ("start",)

    def render_panel(self) -> None:
        lines = [
            scheduler_summary_text(self.scheduler),
            "",
            "Actions: " + ", ".join(_action_label(action) for action in self.available_actions()),
        ]
        if self.confirmation_text:
            lines.extend(["", self.confirmation_text])
        if self.error_message is not None:
            lines.extend(["", f"Error: {self.error_message}"])
        text = "\n".join(lines)
        self.content_text = text
        if self.is_mounted:
            self.query_one("#scheduler-panel-content", Static).update(text)


def scheduler_summary_text(scheduler: SchedulerSummary) -> str:
    return (
        "Scheduler\n"
        f"  Mode: {scheduler.mode.upper()}\n"
        f"  Lease: {scheduler.lease_state}\n"
        f"  Runner: {scheduler.runner_id or 'none'}\n"
        f"  Cancel pending: {scheduler.cancel_pending}\n"
        f"  Hold pending: {scheduler.hold_pending}"
    )


def scheduler_confirmation_text(action: str) -> str:
    return {
        "pause": (
            "Pause the scheduler?\n\n"
            "Active work will finish.\n"
            "No new jobs will start until resumed."
        ),
        "drain": (
            "Drain and stop the scheduler?\n\n"
            "Active work will finish.\n"
            "No new jobs will start.\n"
            "The scheduler will exit after active work completes."
        ),
        "stop": (
            "Stop the scheduler now?\n\n"
            "Active probe, encode, or validation work will be interrupted safely.\n"
            "Interrupted jobs will remain resumable.\n"
            "Jobs will not be canceled."
        ),
        "start": "Start Scheduler",
    }.get(action, action)


def _action_label(action: str) -> str:
    return {
        "start": "Start Scheduler",
        "pause": "Pause",
        "resume": "Resume",
        "drain": "Drain",
        "stop": "Stop",
        "draining": "Draining...",
        "stopping": "Stopping...",
    }[action]
