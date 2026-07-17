from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from pathlib import Path

from rich.columns import Columns
from rich.console import Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from avarch.application.scheduler_snapshot import (
    ActiveJobSummary,
    AttemptProgressSummary,
    SchedulerSnapshot,
    WorkflowStepState,
    WorkflowStepSummary,
)
from avarch.cli_rendering import UNAVAILABLE, format_compact_duration


class DashboardMode(StrEnum):
    OBSERVER = "observer"
    OWNER = "owner"


def render_scheduler_dashboard(
    snapshot: SchedulerSnapshot,
    *,
    width: int,
    mode: DashboardMode,
) -> RenderableType:
    if width < 90:
        return _compact_dashboard(snapshot, width=width, mode=mode)
    main = Group(
        _header(snapshot, mode=mode),
        _pipeline_panel(snapshot),
        _active_jobs_panel(snapshot),
        _upcoming_panel(snapshot),
    )
    side = Group(
        _capacity_panel(snapshot),
        _resource_panel(snapshot),
        _activity_panel(snapshot),
        _alerts_panel(snapshot),
    )
    if width >= 120:
        return Group(Columns((main, side), equal=False, expand=True))
    return Group(main, side)


def _compact_dashboard(
    snapshot: SchedulerSnapshot,
    *,
    width: int,
    mode: DashboardMode,
) -> Text:
    text = Text()
    _append_line(text, f"Avarch Scheduler · {snapshot.scheduler.state.value} · {mode.value}", width)
    _append_line(text, f"Workspace {_fit(snapshot.workspace.root_path, width - 10)}", width)
    pipeline = snapshot.pipeline
    _append_line(
        text,
        (
            f"Pipeline queued {pipeline.queued} active {pipeline.active} "
            f"completed {pipeline.completed} failed {pipeline.failed}"
        ),
        width,
    )
    if snapshot.active_jobs:
        _append_line(text, "Active", width)
        for job in snapshot.active_jobs:
            _append_line(
                text,
                (
                    f"- {_fit(_display_path(job.source_path), 24)} "
                    f"{job.profile_name or UNAVAILABLE} {job.stage.value} "
                    f"{_progress(job.attempt)}"
                ),
                width,
            )
    else:
        _append_line(text, "Active none", width)
    if snapshot.upcoming_jobs:
        _append_line(text, "Upcoming", width)
        for job in snapshot.upcoming_jobs:
            _append_line(
                text,
                (
                    f"#{job.selection_position or '-'} "
                    f"{_fit(_display_path(job.source_path), 28)} "
                    f"{job.profile_name or UNAVAILABLE} {job.stage.value}"
                ),
                width,
            )
    else:
        _append_line(text, "Upcoming none", width)
    _append_line(
        text,
        (
            f"Capacity encode slots {snapshot.capacity.av1an_active}/"
            f"{snapshot.capacity.av1an_jobs} file operations "
            f"{snapshot.capacity.file_ops_active}/{snapshot.capacity.file_ops}"
        ),
        width,
    )
    _append_line(text, "Resource telemetry unavailable", width)
    _append_line(text, "Recent activity unavailable", width)
    return text


def _header(snapshot: SchedulerSnapshot, *, mode: DashboardMode) -> Panel:
    title = Text("Avarch Scheduler", style="bold blue")
    body = Table.grid(padding=(0, 2))
    body.add_column()
    body.add_column()
    body.add_row("Workspace", snapshot.workspace.root_path)
    body.add_row("Scheduler", snapshot.scheduler.state.value)
    body.add_row("Captured", snapshot.captured_at.isoformat(sep=" ", timespec="seconds"))
    body.add_row("Mode", mode.value)
    return Panel(Group(title, body), title="Overview", border_style="blue")


def _pipeline_panel(snapshot: SchedulerSnapshot) -> Panel:
    table = Table.grid(padding=(0, 2))
    table.add_column(style="bold")
    table.add_column(justify="right")
    pipeline = snapshot.pipeline
    for label, value in (
        ("queued", pipeline.queued),
        ("active", pipeline.active),
        ("completed", pipeline.completed),
        ("failed", pipeline.failed),
        ("validation failed", pipeline.validation_failed),
        ("size rejected", pipeline.size_rejected),
        ("cancelled", pipeline.cancelled),
        ("held", pipeline.held),
    ):
        table.add_row(label, str(value))
    return Panel(table, title="Pipeline", border_style="green")


def _active_jobs_panel(snapshot: SchedulerSnapshot) -> Panel:
    if not snapshot.active_jobs:
        return Panel("No active jobs", title="Active Jobs", border_style="grey50")

    table = Table(expand=True, show_lines=False)
    table.add_column("File", overflow="fold")
    table.add_column("Profile")
    table.add_column("Workflow")
    table.add_column("Progress")
    for job in snapshot.active_jobs:
        table.add_row(
            _display_path(job.source_path),
            job.profile_name or UNAVAILABLE,
            _workflow_steps(job.workflow_steps),
            _progress(job.attempt),
        )
    return Panel(table, title="Active Jobs", border_style="yellow")


def _upcoming_panel(snapshot: SchedulerSnapshot) -> Panel:
    if not snapshot.upcoming_jobs:
        return Panel("No upcoming jobs", title="Upcoming", border_style="grey50")
    table = Table(expand=True)
    table.add_column("#", justify="right")
    table.add_column("File", overflow="fold")
    table.add_column("Profile")
    table.add_column("Stage")
    for job in snapshot.upcoming_jobs:
        table.add_row(
            str(job.selection_position or ""),
            _display_path(job.source_path),
            job.profile_name or UNAVAILABLE,
            job.stage.value,
        )
    return Panel(table, title="Upcoming Jobs", border_style="blue")


def _capacity_panel(snapshot: SchedulerSnapshot) -> Panel:
    capacity = snapshot.capacity
    table = Table.grid(padding=(0, 2))
    table.add_column()
    table.add_column(justify="right")
    table.add_row("cheap workers", f"{capacity.cheap_active}/{capacity.cheap_workers}")
    table.add_row("encode slots", f"{capacity.av1an_active}/{capacity.av1an_jobs}")
    table.add_row("file operations", f"{capacity.file_ops_active}/{capacity.file_ops}")
    workers = (
        f"{capacity.av1an_workers_configured} configured"
        if capacity.av1an_workers_configured is not None
        else UNAVAILABLE
    )
    table.add_row("Av1an workers", workers)
    return Panel(table, title="Capacity", border_style="blue")


def _resource_panel(snapshot: SchedulerSnapshot) -> Panel:
    if snapshot.resources is None:
        return Panel("Resource telemetry unavailable", title="Resources", border_style="grey50")
    return Panel(str(snapshot.resources), title="Resources", border_style="green")


def _activity_panel(snapshot: SchedulerSnapshot) -> Panel:
    if not snapshot.recent_events:
        return Panel("Recent activity unavailable", title="Recent Activity", border_style="grey50")
    return Panel("\n".join(str(event) for event in snapshot.recent_events), title="Recent Activity")


def _alerts_panel(snapshot: SchedulerSnapshot) -> Panel:
    if not snapshot.alerts:
        return Panel("No alerts", title="Alerts", border_style="green")
    text = Text()
    for alert in snapshot.alerts:
        style = "red" if alert.severity.value == "error" else "yellow"
        text.append(f"{alert.code}: {alert.message}\n", style=style)
    return Panel(text, title="Alerts", border_style="yellow")


def _workflow_steps(steps: tuple[WorkflowStepSummary, ...]) -> Text:
    text = Text()
    for index, step in enumerate(steps):
        if index:
            text.append(" > ", style="grey50")
        text.append(step.stage.value, style=_step_style(step.state))
    return text


def _step_style(state: WorkflowStepState) -> str:
    return {
        WorkflowStepState.PENDING: "grey50",
        WorkflowStepState.ACTIVE: "yellow",
        WorkflowStepState.COMPLETE: "green",
        WorkflowStepState.FAILED: "red",
        WorkflowStepState.BLOCKED: "red",
        WorkflowStepState.SKIPPED: "grey50",
    }[state]


def _progress(progress: AttemptProgressSummary | None) -> str:
    if progress is None:
        return UNAVAILABLE
    parts: list[str] = []
    if progress.frames_current is not None:
        if progress.frames_total is not None:
            parts.append(f"{progress.frames_current}/{progress.frames_total} frames")
        else:
            parts.append(f"{progress.frames_current} frames")
    if progress.rate_per_second is not None:
        parts.append(f"{progress.rate_per_second:.1f} fps")
    if progress.speed_ratio is not None:
        parts.append(f"{progress.speed_ratio:.2f}x")
    if progress.eta_seconds is not None:
        parts.append(f"ETA {format_compact_duration(timedelta(seconds=progress.eta_seconds))}")
    if progress.elapsed_seconds is not None:
        parts.append(
            f"elapsed {format_compact_duration(timedelta(seconds=progress.elapsed_seconds))}"
        )
    return " · ".join(parts) if parts else UNAVAILABLE


def _display_path(path: str) -> str:
    return Path(path).name or path


def _append_line(text: Text, value: str, width: int) -> None:
    text.append(_fit(value, width))
    text.append("\n")


def _fit(value: str, width: int) -> str:
    if width <= 1:
        return ""
    if len(value) <= width:
        return value
    if width <= 3:
        return value[:width]
    return value[: width - 3] + "..."
