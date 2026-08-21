from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import typer

from avarch.application.job_views import JobAttemptView, JobListItem
from avarch.application.progress_views import JobProgressView
from avarch.application.promotion import PromotionPreflightView, PromotionRecordView
from avarch.domain.jobs import JobOutcomeReason, JobStatus
from avarch.domain.progress import ProgressSource
from avarch.models.promotion import PromotionMode

UNAVAILABLE = "—"


@dataclass(frozen=True, slots=True)
class ProgressWatchRenderMode:
    live: bool
    color: bool


def format_size(size_bytes: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    size = float(size_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size_bytes} B"


def display_optional(value: str | None) -> str:
    return value if value is not None else "unknown"


def display_optional_datetime(value: datetime | None) -> str:
    if value is None:
        return UNAVAILABLE
    return value.isoformat(sep=" ", timespec="seconds")


def job_control_label(job: JobListItem) -> str:
    if job.cancel_requested_at is not None and job_status_value(job.status) != JobStatus.CANCELLED:
        return "cancel"
    if job.hold_requested_at is not None:
        return "hold"
    if job_status_value(job.status) == JobStatus.HELD.value:
        return "held"
    return UNAVAILABLE


def job_progress_phase_label(view: JobProgressView | None) -> str:
    if view is None or view.phase is None:
        return UNAVAILABLE
    return view.phase.value


def job_progress_compact_label(view: JobProgressView | None) -> str:
    if view is None or view.phase is None:
        return UNAVAILABLE
    if view.percent is not None:
        return f"{view.percent:.1f}%"
    if view.current is not None and view.unit is not None:
        return f"{_format_number(view.current)} {view.unit.value}"
    return UNAVAILABLE


def job_progress_eta_label(view: JobProgressView | None) -> str:
    if view is None or view.eta is None:
        return UNAVAILABLE
    return format_compact_duration(view.eta)


def job_progress_updated_label(view: JobProgressView | None) -> str:
    if view is None or view.heartbeat_age is None:
        return UNAVAILABLE
    if view.heartbeat_stale:
        return "stale"
    return f"{format_compact_duration(view.heartbeat_age)} ago"


def select_progress_watch_render_mode(
    *,
    stdout_is_tty: bool,
    no_color: str | None,
) -> ProgressWatchRenderMode:
    return ProgressWatchRenderMode(live=stdout_is_tty, color=stdout_is_tty and no_color is None)


def plain_job_progress_line(view: JobProgressView, *, label: str) -> str:
    parts = [
        label,
        job_progress_phase_label(view),
        job_progress_compact_label(view),
        f"elapsed={format_compact_duration(view.elapsed)}" if view.elapsed is not None else None,
        f"eta={format_compact_duration(view.eta)}" if view.eta is not None else "eta=unknown",
        f"speed={view.speed_ratio:.2f}x" if view.speed_ratio is not None else None,
        "heartbeat=stale" if view.heartbeat_stale else None,
        "not_advancing" if view.not_advancing else None,
    ]
    return " ".join(part for part in parts if part)


def echo_job_progress_details(view: JobProgressView) -> None:
    typer.echo("Progress:")
    typer.echo(f"  attempt:          {_attempt_identity(view)}")
    typer.echo(f"  phase:            {job_progress_phase_label(view)}")
    typer.echo(f"  phase progress:   {job_progress_detail_label(view)}")
    if view.elapsed is not None:
        typer.echo(f"  elapsed:          {format_compact_duration(view.elapsed)}")
    if view.eta is not None:
        typer.echo(f"  estimated left:   {format_compact_duration(view.eta)}")
    if view.speed_ratio is not None:
        typer.echo(f"  speed:            {view.speed_ratio:.2f}x")
    if view.rate_per_second is not None:
        unit = f" {view.unit.value}" if view.unit is not None else ""
        typer.echo(f"  rate:             {_format_number(view.rate_per_second)}{unit}/s")
    if view.source is not None:
        typer.echo(f"  source:           {_source_label(view.source)}")
    if view.heartbeat_age is not None:
        suffix = " (stale)" if view.heartbeat_stale else ""
        typer.echo(f"  last heartbeat:   {format_compact_duration(view.heartbeat_age)} ago{suffix}")
    if view.advance_age is not None:
        suffix = " (not advancing)" if view.not_advancing else ""
        typer.echo(f"  last advance:     {format_compact_duration(view.advance_age)} ago{suffix}")
    if view.message:
        typer.echo(f"  message:          {truncate_line(view.message)}")


def format_compact_duration(duration: timedelta) -> str:
    seconds = max(0, int(duration.total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes:02d}m"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours:02d}h"


def _attempt_identity(view: JobProgressView) -> str:
    if view.attempt_number is None:
        return UNAVAILABLE
    if view.attempt_id is None:
        return str(view.attempt_number)
    return f"{view.attempt_number} (id {view.attempt_id})"


def job_progress_detail_label(view: JobProgressView) -> str:
    if view.current is None:
        return job_progress_compact_label(view)
    unit = f" {view.unit.value}" if view.unit is not None else ""
    if view.total is None:
        return f"{_format_number(view.current)}{unit}"
    label = f"{_format_number(view.current)} / {_format_number(view.total)}{unit}"
    if view.percent is not None:
        label = f"{label} ({view.percent:.1f}%)"
    return label


def _format_number(value: int | float) -> str:
    if isinstance(value, float) and not value.is_integer():
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(int(value))


def _source_label(source: ProgressSource) -> str:
    if source in {
        ProgressSource.AV1AN_OUTPUT,
        ProgressSource.AV1AN_STATE,
        ProgressSource.AV1AN_STRUCTURED,
    }:
        return "av1an"
    if source == ProgressSource.FFMPEG_PROGRESS:
        return "ffmpeg"
    if source == ProgressSource.PROCESS_HEARTBEAT:
        return "process"
    return source.value


def job_outcome_summary(job: JobListItem, *, path: str) -> str | None:
    status = JobStatus(job.status)
    if status == JobStatus.ENCODING:
        return f"Encoding: {path}"
    if status == JobStatus.VALIDATING:
        return f"Validating: {path}"
    if status == JobStatus.READY_TO_PROMOTE:
        return f"Ready to promote: {path}"
    if status == JobStatus.PROMOTING:
        return f"Promoting: {path}"
    if status == JobStatus.CLEANING:
        return f"Cleaning: {path}"
    if status == JobStatus.PROMOTED:
        return f"Promoted: {path}"
    if status == JobStatus.SIZE_REJECTED:
        reason = JobOutcomeReason(job.outcome_reason) if job.outcome_reason is not None else None
        if reason == JobOutcomeReason.SKIPPED_MINIMUM_SAVINGS_NOT_MET:
            return f"Skipped: {path}, minimum savings was not met"
        return f"Skipped: {path}, output was not smaller"
    if status == JobStatus.VALIDATION_FAILED:
        detail = job.last_error_message or "validation failed"
        return f"Failed: {path}, {truncate_line(detail)}"
    if status == JobStatus.FAILED and job.last_error_message:
        return f"Failed: {path}, {truncate_line(job.last_error_message)}"
    if status == JobStatus.SKIPPED and job.skip_reason:
        return f"Skipped: {path}, {truncate_line(job.skip_reason)}"
    return None


def attempt_status_value(status: object) -> str:
    return str(getattr(status, "value", status))


def event_type_value(event_type: object) -> str:
    return str(getattr(event_type, "value", event_type))


def echo_attempt_log_status(attempt: JobAttemptView, *, indent: str) -> None:
    statuses: list[str] = []
    for label, log_path in (("stdout", attempt.stdout_log), ("stderr", attempt.stderr_log)):
        if log_path is None:
            statuses.append(f"{label}: -")
            continue
        path = Path(log_path)
        suffix = "" if path.exists() else " (missing)"
        statuses.append(f"{label}: {path}{suffix}")
    typer.echo(f"{indent}logs: {', '.join(statuses)}")


def echo_error_block(
    error_type: str | None,
    error_message: str | None,
    *,
    indent: str,
    max_length: int = 2000,
) -> None:
    typer.echo(f"{indent}{error_type or 'Error'}:")
    if not error_message:
        typer.echo(f"{indent}  -")
        return
    for line in truncate_text(error_message, max_length=max_length).splitlines():
        typer.echo(f"{indent}  {line}")


def truncate_text(value: str, *, max_length: int) -> str:
    text = value.strip()
    if len(text) <= max_length:
        return text
    return f"{text[: max_length - 15].rstrip()}\n... truncated ..."


def truncate_line(value: str, *, max_length: int = 120) -> str:
    line = " ".join(value.splitlines()).strip()
    if len(line) <= max_length:
        return line
    return f"{line[: max_length - 3]}..."


def echo_promotion_preview(preflight: PromotionPreflightView, *, dry_run: bool) -> None:
    result = preflight
    typer.echo("Promotion dry run" if dry_run else "Promotion preview")
    typer.echo("")
    typer.echo(f"Job:               {result.job_id}")
    typer.echo(f"Mode:              {result.mode.value}")
    typer.echo(f"Source:            {result.source_path}")
    typer.echo(f"Validated output:  {result.validated_output_path}")
    typer.echo(f"Final path:        {result.final_path}")
    backup_path = result.backup_path if result.backup_path is not None else "none"
    typer.echo(f"Backup path:       {backup_path}")
    typer.echo(f"Staging path:      {result.staging_path}")
    typer.echo("")
    typer.echo("Validation:")
    typer.echo(f"  result:          {result.validation_result_id}")
    typer.echo("  passed:          yes")
    typer.echo("  output unchanged:yes")
    typer.echo("")
    typer.echo("Actions:")
    typer.echo("  1. Hash validated output")
    typer.echo("  2. Copy to destination-local staging")
    typer.echo("  3. Verify staging digest")
    typer.echo("  4. Atomically install final path")
    typer.echo("  5. Verify final digest")
    typer.echo("  6. Commit promotion")
    typer.echo("  7. Clean disposable work files")
    typer.echo("")
    typer.echo("No files were changed.")


def echo_promotion_complete(record: PromotionRecordView) -> None:
    typer.echo("Promotion complete")
    typer.echo("")
    typer.echo(f"Job:               {record.job_id}")
    typer.echo(f"Mode:              {promotion_mode_value(record.mode)}")
    typer.echo(f"Source retained:   {source_retained_path(record)}")
    typer.echo(f"Promoted output:   {record.final_path}")
    typer.echo(f"Validation result: {record.validation_result_id}")
    typer.echo(f"Promotion record:  {record.id}")
    typer.echo(f"Cleanup:           {'complete' if record.cleanup_completed else 'warning'}")
    if record.cleanup_error:
        typer.echo(f"Cleanup warning:   {record.cleanup_error}")


def job_status_value(status: JobStatus | str) -> str:
    if isinstance(status, JobStatus):
        return status.value
    return status


def job_stage_value(stage: object) -> str:
    return str(getattr(stage, "value", stage))


def promotion_mode_value(mode: object) -> str:
    return str(getattr(mode, "value", mode))


def source_retained_path(record: PromotionRecordView) -> str:
    mode = promotion_mode_value(record.mode)
    if mode == PromotionMode.KEEP_ORIGINAL.value:
        return record.source_path
    if mode == PromotionMode.MOVE_ORIGINAL_TO_BACKUP.value and record.backup_path is not None:
        return record.backup_path
    if (
        mode == PromotionMode.REPLACE_ATOMIC.value
        and not record.cleanup_completed
        and record.backup_path is not None
    ):
        return record.backup_path
    return "none"
