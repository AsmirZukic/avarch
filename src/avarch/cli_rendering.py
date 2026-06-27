from __future__ import annotations

from datetime import datetime
from pathlib import Path

import typer

from avarch.application.job_views import JobAttemptView, JobListItem
from avarch.application.promotion import PromotionPreflightView, PromotionRecordView
from avarch.domain.jobs import JobOutcomeReason, JobStatus
from avarch.models.promotion import PromotionMode


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
        return "-"
    return value.isoformat(sep=" ", timespec="seconds")


def job_control_label(job: JobListItem) -> str:
    if job.cancel_requested_at is not None and job_status_value(job.status) != JobStatus.CANCELLED:
        return "cancel"
    if job.hold_requested_at is not None:
        return "hold"
    if job_status_value(job.status) == JobStatus.HELD.value:
        return "held"
    return "-"


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
    if record.backup_path is not None:
        return record.backup_path
    return "none"
