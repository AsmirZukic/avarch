from __future__ import annotations

from datetime import timedelta
from enum import StrEnum
from pathlib import Path

from rich.console import Group, RenderableType
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from avarch.application.resource_telemetry import ResourceHealth
from avarch.application.scheduler_snapshot import (
    AttemptProgressSummary,
    BlockedJobSummary,
    LifecycleEventSummary,
    ResourceMetricSummary,
    ResourceTelemetrySummary,
    SchedulerSessionRunSummary,
    SchedulerSnapshot,
    WorkflowStepState,
    WorkflowStepSummary,
)
from avarch.cli_rendering import UNAVAILABLE, format_compact_duration, format_size


class DashboardMode(StrEnum):
    OBSERVER = "observer"
    OWNER = "owner"


def render_scheduler_dashboard(
    snapshot: SchedulerSnapshot,
    *,
    width: int,
    mode: DashboardMode,
    shortcuts_available: bool = True,
    height: int | None = None,
) -> RenderableType:
    if width < 90:
        return _compact_dashboard(
            snapshot,
            width=width,
            mode=mode,
            shortcuts_available=shortcuts_available,
        )
    main = Group(
        _pipeline_panel(snapshot),
        _active_jobs_panel(snapshot),
        _details_panel(snapshot) if snapshot.watch_details is not None else "",
        _log_tail_panel(snapshot) if snapshot.watch_log_tail is not None else "",
        _upcoming_panel(snapshot),
        _blocked_panel(snapshot),
    )
    side = Group(
        _capacity_panel(snapshot),
        _session_panel(snapshot),
        _resource_panel(snapshot),
        _activity_panel(snapshot),
        _alerts_panel(snapshot),
    )
    header = _header(snapshot, mode=mode)
    footer = _footer(mode, shortcuts_available=shortcuts_available)
    if width >= 120 and height is not None:
        return _layout_dashboard(header=header, main=main, side=side, footer=footer)
    if width >= 120:
        return Group(header, _grid_body(main, side), footer)
    return Group(header, main, side, footer)


def _layout_dashboard(
    *,
    header: RenderableType,
    main: RenderableType,
    side: RenderableType,
    footer: RenderableType,
) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(header, name="header", size=6),
        Layout(name="body"),
        Layout(footer, name="footer", size=3),
    )
    layout["body"].split_row(
        Layout(main, name="main", ratio=3, minimum_size=72),
        Layout(side, name="side", ratio=2, minimum_size=44),
    )
    return layout


def _grid_body(main: RenderableType, side: RenderableType) -> Table:
    grid = Table.grid(expand=True, padding=(0, 1))
    grid.add_column(ratio=3)
    grid.add_column(ratio=2)
    grid.add_row(main, side)
    return grid


def _compact_dashboard(
    snapshot: SchedulerSnapshot,
    *,
    width: int,
    mode: DashboardMode,
    shortcuts_available: bool,
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
    _append_line(text, _resource_summary(snapshot.resources), width)
    if snapshot.blocked_jobs:
        _append_line(text, f"Blocked {len(snapshot.blocked_jobs)}", width)
        for job in snapshot.blocked_jobs[:3]:
            _append_line(
                text,
                f"- {_display_path(job.source_path)} {job.reason.value.replace('_', ' ')}",
                width,
            )
    if snapshot.session is not None and snapshot.session.current is not None:
        session = snapshot.session.current
        _append_line(
            text,
            f"Session {_session_line(session)}",
            width,
        )
    if snapshot.recent_events:
        _append_line(text, "Recent", width)
        for event in snapshot.recent_events[:3]:
            _append_line(text, f"- {_event_line(event)}", width)
    else:
        _append_line(text, "Recent activity unavailable", width)
    _append_line(text, _footer_text(mode, shortcuts_available=shortcuts_available), width)
    return text


def _footer(mode: DashboardMode, *, shortcuts_available: bool) -> Panel:
    return Panel(
        _footer_text(mode, shortcuts_available=shortcuts_available),
        title="Controls",
        border_style="grey50",
    )


def _footer_text(mode: DashboardMode, *, shortcuts_available: bool) -> str:
    if mode == DashboardMode.OWNER:
        return "Ctrl+C stop scheduler"
    if not shortcuts_available:
        return "Ctrl+C detach · interactive shortcuts unavailable"
    return "p pause   c cancel   l logs   Enter details   q detach"


def _header(snapshot: SchedulerSnapshot, *, mode: DashboardMode) -> Panel:
    title = Text("Avarch Scheduler", style="bold blue")
    body = Table.grid(padding=(0, 2))
    body.add_column()
    body.add_column()
    body.add_row("Workspace", snapshot.workspace.root_path)
    body.add_row("Scheduler", snapshot.scheduler.state.value)
    body.add_row("Captured", snapshot.captured_at.isoformat(sep=" ", timespec="seconds"))
    body.add_row("Mode", mode.value)
    return Panel(Group(title, body), title="Overview", border_style="grey50")


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
    return Panel(table, title="Pipeline", border_style="grey50")


def _active_jobs_panel(snapshot: SchedulerSnapshot) -> Panel:
    if not snapshot.active_jobs:
        return Panel("No active jobs", title="Active Jobs", border_style="grey50")

    table = Table.grid(expand=True, padding=(0, 2))
    table.add_column(style="bold", width=10)
    table.add_column(ratio=1)
    for job in snapshot.active_jobs:
        table.add_row("File", _display_path(job.source_path))
        table.add_row("Profile", job.profile_name or UNAVAILABLE)
        table.add_row("Workflow", _workflow_steps(job.workflow_steps))
        table.add_row("Progress", _progress(job.attempt))
        if job != snapshot.active_jobs[-1]:
            table.add_row("", "")
    return Panel(table, title="Active Jobs", border_style="yellow")


def _upcoming_panel(snapshot: SchedulerSnapshot) -> Panel:
    if not snapshot.upcoming_jobs:
        return Panel("No upcoming jobs", title="Upcoming", border_style="grey50")
    renderables: list[RenderableType] = []
    if snapshot.forecast is not None:
        renderables.append(Text(_forecast_line(snapshot)))
    table = Table(expand=True)
    table.add_column("#", justify="right")
    table.add_column("File", overflow="fold")
    table.add_column("Profile")
    table.add_column("Stage")
    table.add_column("Estimate")
    for job in snapshot.upcoming_jobs:
        table.add_row(
            str(job.selection_position or ""),
            _display_path(job.source_path),
            job.profile_name or UNAVAILABLE,
            f"{job.stage.value} · {job.selection_confidence or UNAVAILABLE}",
            _upcoming_start(job),
        )
    renderables.append(table)
    return Panel(Group(*renderables), title="Upcoming Jobs", border_style="grey50")


def _capacity_panel(snapshot: SchedulerSnapshot) -> Panel:
    capacity = snapshot.capacity
    table = Table.grid(padding=(0, 2))
    table.add_column()
    table.add_column(justify="right")
    table.add_row("light workers", f"{capacity.cheap_active}/{capacity.cheap_workers}")
    table.add_row("encode slots", f"{capacity.av1an_active}/{capacity.av1an_jobs}")
    table.add_row("file operations", f"{capacity.file_ops_active}/{capacity.file_ops}")
    if capacity.stale:
        table.add_row("freshness", "stale")
    workers = (
        f"{capacity.av1an_workers_configured} configured"
        if capacity.av1an_workers_configured is not None
        else UNAVAILABLE
    )
    table.add_row("Av1an workers", workers)
    return Panel(table, title="Capacity", border_style="grey50")


def _blocked_panel(snapshot: SchedulerSnapshot) -> Panel:
    if not snapshot.blocked_jobs:
        return Panel("No blocked jobs", title="Blocked", border_style="grey50")
    grouped: dict[str, list[BlockedJobSummary]] = {}
    for job in snapshot.blocked_jobs:
        grouped.setdefault(job.reason.value, []).append(job)
    table = Table(expand=True)
    table.add_column("Reason")
    table.add_column("Count", justify="right")
    table.add_column("Examples", overflow="fold")
    for reason, jobs in grouped.items():
        table.add_row(
            reason.replace("_", " "),
            str(len(jobs)),
            ", ".join(_display_path(job.source_path) for job in jobs[:3]),
        )
    return Panel(table, title=f"Blocked {len(snapshot.blocked_jobs)}", border_style="yellow")


def _details_panel(snapshot: SchedulerSnapshot) -> Panel:
    details = snapshot.watch_details
    if details is None:
        return Panel("No selected job", title="Details", border_style="grey50")
    table = Table.grid(padding=(0, 2))
    table.add_column()
    table.add_column()
    table.add_row("job ID", str(details.job_id))
    table.add_row("attempt", str(details.attempt_number or UNAVAILABLE))
    table.add_row("source", details.source_path)
    table.add_row("profile", details.profile_name or UNAVAILABLE)
    table.add_row("stage", details.stage.value)
    if details.started_at is not None:
        table.add_row("start time", details.started_at.isoformat(sep=" ", timespec="seconds"))
    table.add_row("plan", details.plan_path or UNAVAILABLE)
    table.add_row("output", details.output_path or UNAVAILABLE)
    if details.last_error_type or details.last_error_message:
        error = " ".join(
            part for part in (details.last_error_type, details.last_error_message) if part
        )
        table.add_row(
            "last error",
            error,
        )
    return Panel(table, title="Details", border_style="grey50")


def _log_tail_panel(snapshot: SchedulerSnapshot) -> Panel:
    tail = snapshot.watch_log_tail
    if tail is None:
        return Panel("No log selected", title="Logs", border_style="grey50")
    if tail.missing:
        return Panel("Log unavailable", title="Logs", border_style="grey50")
    text = Text()
    for line in tail.lines:
        text.append(line)
        text.append("\n")
    return Panel(text, title="Logs", border_style="grey50")


def _resource_panel(snapshot: SchedulerSnapshot) -> Panel:
    if snapshot.resources is None:
        return Panel("Resource telemetry unavailable", title="Resources", border_style="grey50")
    resources = snapshot.resources
    if resources.health == ResourceHealth.UNAVAILABLE:
        return Panel("Resource telemetry unavailable", title="Resources", border_style="grey50")
    table = Table.grid(padding=(0, 2))
    table.add_column()
    table.add_column(justify="right")
    table.add_column()
    for metric in resources.metrics:
        table.add_row(
            _resource_label(metric),
            _resource_value(metric),
            _resource_status(metric),
        )
    if resources.stale:
        table.add_row("freshness", "stale", "")
    return Panel(table, title="Resources", border_style=_resource_border(resources.health))


def _forecast_line(snapshot: SchedulerSnapshot) -> str:
    forecast = snapshot.forecast
    if forecast is None:
        return UNAVAILABLE
    if not forecast.available:
        return f"forecast unavailable · {_forecast_reason(forecast.reason)}"
    if forecast.lower_seconds is None or forecast.upper_seconds is None:
        return "forecast unavailable · incomplete estimate"
    return (
        f"forecast {_forecast_range(forecast.lower_seconds, forecast.upper_seconds)} · "
        f"{forecast.confidence.value} confidence · n={forecast.sample_count}"
    )


def _upcoming_start(job: object) -> str:
    lower = getattr(job, "estimated_start_lower_seconds", None)
    upper = getattr(job, "estimated_start_upper_seconds", None)
    if lower is None or upper is None:
        return UNAVAILABLE
    return f"starts in {_forecast_range(lower, upper)}"


def _forecast_range(lower_seconds: int, upper_seconds: int) -> str:
    lower = _forecast_duration(lower_seconds)
    upper = _forecast_duration(upper_seconds)
    return lower if lower == upper else f"{lower}-{upper}"


def _forecast_duration(seconds: int) -> str:
    seconds = max(0, seconds)
    hours = seconds // 3600
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h"
    minutes = seconds // 60
    return f"{minutes}m" if minutes > 0 else f"{seconds}s"


def _forecast_reason(reason: str | None) -> str:
    if reason == "insufficient_history":
        return "insufficient history"
    if reason == "multi_lane_capacity_not_supported":
        return "multi-lane capacity unsupported"
    if reason is None:
        return UNAVAILABLE
    return reason.replace("_", " ")


def _session_panel(snapshot: SchedulerSnapshot) -> Panel:
    if snapshot.session is None:
        return Panel("No scheduler sessions", title="Session", border_style="grey50")
    table = Table.grid(padding=(0, 2))
    table.add_column()
    table.add_column()
    if snapshot.session.current is None:
        table.add_row("current", UNAVAILABLE)
    else:
        current = snapshot.session.current
        table.add_row("current", _session_line(current))
        table.add_row("started", current.started_at.isoformat(sep=" ", timespec="seconds"))
    previous = [
        _session_line(run)
        for run in snapshot.session.recent
        if snapshot.session.current is None or run.session_id != snapshot.session.current.session_id
    ]
    if previous:
        table.add_row("previous", "\n".join(previous[:3]))
    return Panel(table, title="Session", border_style="grey50")


def _activity_panel(snapshot: SchedulerSnapshot) -> Panel:
    if not snapshot.recent_events:
        return Panel("Recent activity unavailable", title="Recent Activity", border_style="grey50")
    text = Text()
    for event in snapshot.recent_events:
        details = _event_details(event)
        line = f"{event.created_at.strftime('%H:%M:%S')}  job {event.job_id} {_event_label(event)}"
        if details != UNAVAILABLE:
            line = f"{line}  {details}"
        text.append(line)
        text.append("\n")
    return Panel(text, title="Recent Activity", border_style="grey50")


def _session_line(session: SchedulerSessionRunSummary) -> str:
    state = "active" if session.active else session.end_reason or "ended"
    pid = str(session.pid) if session.pid is not None else UNAVAILABLE
    return f"{_short_id(session.owner_id)} on {_short_id(session.host)} pid {pid} ({state})"


def _event_line(event: LifecycleEventSummary) -> str:
    return f"job {event.job_id} {_event_label(event)} {_event_details(event)}".rstrip()


def _event_label(event: LifecycleEventSummary) -> str:
    stage = event.stage.value if event.stage is not None else "stage"
    labels = {
        "stage_started": f"{stage} started",
        "stage_completed": f"{stage} completed",
        "stage_failed": f"{stage} failed",
        "stage_cancelled": f"{stage} cancelled",
        "hold_requested": "hold requested",
        "held": "held",
        "hold_released": "hold released",
        "cancel_requested": "cancel requested",
        "canceled": "cancelled",
        "retry_requested": "retry requested",
        "priority_changed": "priority changed",
        "queue_cleared": "queue cleared",
    }
    return labels.get(event.event_type.value, event.event_type.value.replace("_", " "))


def _event_details(event: LifecycleEventSummary) -> str:
    parts: list[str] = []
    if event.reason:
        parts.append(event.reason)
    saved_bytes = event.details.get("saved_bytes")
    if isinstance(saved_bytes, int):
        parts.append(f"saved {format_size(saved_bytes)}")
    error_type = event.details.get("error_type")
    if isinstance(error_type, str):
        parts.append(error_type)
    size_decision = event.details.get("size_decision")
    if isinstance(size_decision, str):
        parts.append(size_decision.replace("_", " "))
    return " · ".join(parts) if parts else UNAVAILABLE


def _alerts_panel(snapshot: SchedulerSnapshot) -> Panel:
    if not snapshot.alerts:
        return Panel("No alerts", title="Alerts", border_style="grey50")
    text = Text()
    for alert in snapshot.alerts:
        style = "red" if alert.severity.value == "error" else "yellow"
        text.append(f"{alert.code}: {alert.message}\n", style=style)
    return Panel(text, title="Alerts", border_style="yellow")


def _resource_summary(resources: ResourceTelemetrySummary | None) -> str:
    if resources is None or resources.health == ResourceHealth.UNAVAILABLE:
        return "Resource telemetry unavailable"
    parts = [_resource_compact(metric) for metric in resources.metrics if metric.available]
    if resources.stale:
        parts.append("stale")
    return "Resources " + " · ".join(parts) if parts else "Resource telemetry unavailable"


def _resource_compact(metric: ResourceMetricSummary) -> str:
    label = _resource_label(metric)
    value = _resource_value(metric)
    return f"{label} {value}"


def _resource_label(metric: ResourceMetricSummary) -> str:
    if metric.name == "cpu":
        return "CPU"
    if metric.name == "output write rate":
        return "output write"
    return metric.name


def _resource_value(metric: ResourceMetricSummary) -> str:
    if not metric.available:
        return UNAVAILABLE
    if metric.value is None:
        return UNAVAILABLE
    if metric.name == "cpu" and metric.unit == "percent":
        return f"{metric.value:.0f}%"
    if metric.name == "memory" and metric.unit == "bytes" and metric.total is not None:
        return f"{format_size(int(metric.value))}/{format_size(int(metric.total))}"
    if metric.name == "output write rate" and metric.unit == "bytes_per_second":
        return f"{format_size(int(metric.value))}/s"
    if metric.unit:
        return f"{metric.value} {metric.unit}"
    return str(metric.value)


def _resource_status(metric: ResourceMetricSummary) -> str:
    if not metric.available:
        return metric.reason or UNAVAILABLE
    if metric.name in {"cpu", "output write rate"}:
        return "active"
    if metric.health != ResourceHealth.OK:
        return metric.health.value
    return ""


def _resource_border(health: ResourceHealth) -> str:
    if health == ResourceHealth.CRITICAL:
        return "red"
    if health == ResourceHealth.WARNING:
        return "yellow"
    return "green"


def _workflow_steps(steps: tuple[WorkflowStepSummary, ...]) -> Text:
    text = Text()
    for index, step in enumerate(steps):
        if index:
            text.append("  ", style="grey50")
        text.append(_step_glyph(step.state), style=_step_style(step.state))
        text.append(f" {step.stage.value}", style=_step_style(step.state))
    return text


def _step_glyph(state: WorkflowStepState) -> str:
    return {
        WorkflowStepState.PENDING: "○",
        WorkflowStepState.ACTIVE: "●",
        WorkflowStepState.COMPLETE: "✓",
        WorkflowStepState.FAILED: "✕",
        WorkflowStepState.BLOCKED: "!",
        WorkflowStepState.SKIPPED: "○",
    }[state]


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
    if progress.frames_current is not None and progress.frames_total:
        percent = min(1.0, max(0.0, progress.frames_current / progress.frames_total))
        parts.append(f"{_gauge(percent)} {percent * 100:.0f}%")
    if progress.stale and progress.last_update_age_seconds is not None:
        age = format_compact_duration(timedelta(seconds=progress.last_update_age_seconds))
        parts.append(f"last update {age} ago")
    if progress.frames_current is not None:
        if progress.frames_total is not None:
            parts.append(f"{progress.frames_current}/{progress.frames_total} frames")
        else:
            parts.append(f"{progress.frames_current} frames")
    if progress.rate_per_second is not None:
        parts.append(f"{progress.rate_per_second:.1f} fps")
    if progress.speed_ratio is not None:
        parts.append(f"{progress.speed_ratio:.2f}x")
    if progress.chunks_current is not None:
        if progress.chunks_total is not None:
            parts.append(f"{progress.chunks_current}/{progress.chunks_total} chunks")
        else:
            parts.append(f"{progress.chunks_current} chunks")
    if progress.bitrate_kbps is not None:
        parts.append(f"{progress.bitrate_kbps} Kbps")
    if progress.estimated_output_bytes is not None:
        parts.append(f"est. {format_size(progress.estimated_output_bytes)}")
    if progress.written_output_bytes is not None:
        parts.append(f"written {format_size(progress.written_output_bytes)}")
    if progress.eta_seconds is not None:
        parts.append(f"ETA {format_compact_duration(timedelta(seconds=progress.eta_seconds))}")
    if progress.elapsed_seconds is not None:
        parts.append(
            f"elapsed {format_compact_duration(timedelta(seconds=progress.elapsed_seconds))}"
        )
    if not parts:
        return UNAVAILABLE
    if len(parts) > 5:
        return f"{' · '.join(parts[:5])}\n{' · '.join(parts[5:])}"
    return " · ".join(parts)


def _display_path(path: str) -> str:
    return Path(path).name or path


def _gauge(percent: float, *, width: int = 18) -> str:
    filled = round(percent * width)
    return "█" * filled + "░" * (width - filled)


def _short_id(value: str, *, width: int = 8) -> str:
    if len(value) <= width:
        return value
    return f"{value[:width]}…"


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
