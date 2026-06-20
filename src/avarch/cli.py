from __future__ import annotations

import ast
import asyncio
import os
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

import structlog
import typer
from pydantic import ValidationError
from sqlalchemy import inspect
from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from avarch import __version__
from avarch.config import (
    WORKSPACE_CONFIG_TEXT,
    AppConfig,
    load_config,
    resolve_data_dir,
    resolve_database_url,
)
from avarch.db import (
    UnsupportedDatabaseSchemaError,
    create_db_engine,
    verify_database_revision,
)
from avarch.db_migrations import get_current_revision, upgrade_database
from avarch.execution import (
    build_av1an_command,
    build_ffmpeg_mux_command,
    create_mux_temporary_path,
    execute_plan,
)
from avarch.logging import configure_logging
from avarch.models.db import (
    Job,
    JobAttempt,
    JobEvent,
    MediaFile,
    MediaFileStatus,
    PromotionRecord,
    ValidationResult,
)
from avarch.models.execution import ExecutionError
from avarch.models.plan import TranscodePlan
from avarch.models.promotion import PromotionMode, PromotionStatus
from avarch.models.scheduler import JobStage, JobStatus
from avarch.models.validation import ValidationReport
from avarch.planner import (
    PlanArtifactConflictError,
    PlanningError,
    build_plan,
    load_planning_context,
    write_plan_artifacts,
)
from avarch.probe import (
    ProbeError,
    format_probe_summary,
    get_canonical_probe_result,
    normalize_probe,
    parse_normalized_probe_json,
    run_ffprobe,
    store_probe_result,
)
from avarch.profiles.models import EncodingProfile, ProfileDocument
from avarch.profiles.registry import (
    ProfileOrigin,
    ProfileRegistry,
    ProfileRegistryError,
    UnknownProfileError,
)
from avarch.promoter import (
    PromotionError,
    PromotionPreflightResult,
    execute_promotion,
    recover_promotion,
    validate_promotion_preflight,
)
from avarch.scanner import ScanError, ScanResult, scan_root, update_inventory
from avarch.scheduler import (
    MAX_CLI_LOG_TAIL_BYTES,
    JobControlError,
    JobPreparationError,
    SchedulerAlreadyRunningError,
    SchedulerControlError,
    cancel_job,
    clear_queue,
    cli_actor,
    drain_scheduler,
    enqueue_inventory,
    execute_validation_job,
    hold_job,
    new_runner_id,
    pause_scheduler,
    release_job,
    resume_scheduler,
    retry_job,
    retry_queue,
    run_scheduler,
    scheduler_status,
    stop_scheduler,
    update_job_priority,
)
from avarch.validation import format_validation_report_summary
from avarch.vapoursynth import (
    VapourSynthGenerationError,
    VapourSynthScriptPathError,
    VspipeError,
    check_vapoursynth_script,
    generate_vapoursynth_script,
    resolve_vapoursynth_filter,
    resolve_vapoursynth_template,
    resolve_workspace_script_path,
    validate_script_syntax,
)
from avarch.vpy_env import (
    VpyEnvironmentError,
    VpyRequirements,
    add_python_package,
    add_vsrepo_package,
    build_runtime_identity,
    load_requirements,
    remove_python_package,
    remove_vsrepo_package,
    runtime_environment_variables,
    sync_environment,
)
from avarch.vpy_plugins import VpyPluginInventoryError, list_vapoursynth_plugins
from avarch.workspace import (
    WorkspaceContext,
    WorkspaceError,
    create_workspace,
)

log = structlog.get_logger(__name__)

app = typer.Typer(
    name="avarch",
    help="Av1an-first archival transcoding orchestrator.",
    no_args_is_help=True,
)
db_app = typer.Typer(help="Database commands.")
scheduler_app = typer.Typer(help="Scheduler commands.")
jobs_app = typer.Typer(help="Job commands.")
queue_app = typer.Typer(help="Queue commands.")
workspace_app = typer.Typer(help="Workspace commands.")
config_app = typer.Typer(help="Configuration commands.")
workflow_app = typer.Typer(help="Workflow commands.")
profiles_app = typer.Typer(help="Profile commands.")
vpy_app = typer.Typer(help="VapourSynth commands.")
vpy_scaffold_app = typer.Typer(help="VapourSynth scaffolding commands.")
vpy_env_app = typer.Typer(help="VapourSynth environment commands.")
vpy_packages_app = typer.Typer(help="VapourSynth package commands.")


def version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit


VersionOption = Annotated[
    bool,
    typer.Option(
        "--version",
        callback=version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
]
ForceOption = Annotated[bool, typer.Option("--force", help="Overwrite existing config.")]
LogFormatOption = Annotated[
    Literal["console", "json"] | None,
    typer.Option("--log-format", help="Override configured log format."),
]
RootsArgument = Annotated[
    list[Path] | None,
    typer.Argument(help="Media roots to scan."),
]


@app.callback()
def main(version: VersionOption = False) -> None:
    pass


@app.command()
def version() -> None:
    typer.echo(__version__)


@app.command()
def init(force: ForceOption = False) -> None:
    _init_workspace(force=force)


def _init_workspace(*, force: bool) -> None:
    workspace_root = _resolve_cli_path(Path(".")).resolve()
    try:
        workspace = create_workspace(workspace_root, force=force)
    except WorkspaceError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc

    workspace.config_toml.write_text(WORKSPACE_CONFIG_TEXT, encoding="utf-8")
    app_config = load_config(workspace.config_toml)
    configure_logging(app_config.logging.level, app_config.logging.format)

    database_url = resolve_database_url(app_config, workspace.config_toml)
    _upgrade_database_or_exit(database_url)

    profiles = ", ".join(
        profile.name for profile in ProfileRegistry.from_config(app_config).list_profiles()
    )

    typer.echo(f"Workspace: {workspace.root}")
    typer.echo(f"Config: {workspace.config_toml}")
    typer.echo(f"Data dir: {workspace.data_dir}")
    typer.echo(f"Profiles dir: {workspace.profiles_dir}")
    typer.echo(f"Profiles: {profiles}")


@db_app.command("upgrade")
def db_upgrade() -> None:
    config = _workspace_config_path()
    app_config = _load_and_configure(config)
    database_url = resolve_database_url(app_config, config)
    _upgrade_database_or_exit(database_url)
    log.info("database_upgraded", database_url=database_url)
    typer.echo("Database upgraded")


@db_app.command("current")
def db_current() -> None:
    config = _workspace_config_path()
    app_config = _load_and_configure(config)
    database_url = resolve_database_url(app_config, config)
    try:
        engine = create_db_engine(database_url)
        verify_database_revision(engine)
    except UnsupportedDatabaseSchemaError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    revision = get_current_revision(database_url)
    typer.echo(revision or "unknown")


@app.command()
def doctor(
    log_format: LogFormatOption = None,
) -> None:
    config = _workspace_config_path()
    if not config.exists():
        configure_logging()
        _doctor_fail("config_exists", "missing config")
        typer.echo(f"FAIL config_exists missing: {config}")
        raise typer.Exit(1)

    try:
        app_config = load_config(config)
    except Exception as exc:
        configure_logging()
        _doctor_fail("config_parses", exc.__class__.__name__)
        typer.echo("FAIL config_parses")
        raise typer.Exit(1) from exc

    effective_log_format = log_format or app_config.logging.format
    configure_logging(app_config.logging.level, effective_log_format)

    _doctor_pass("config_exists")
    typer.echo("PASS config_exists")
    _doctor_pass("config_parses")
    typer.echo("PASS config_parses")

    data_dir = resolve_data_dir(app_config, config)
    if not data_dir.exists():
        _doctor_fail("data_dir_exists", "missing data directory")
        typer.echo("FAIL data_dir_exists")
        raise typer.Exit(1)
    _doctor_pass("data_dir_exists")
    typer.echo("PASS data_dir_exists")

    database_url = resolve_database_url(app_config, config)
    if not database_url.startswith("sqlite:///"):
        _doctor_fail("database_url_sqlite", "database URL is not SQLite")
        typer.echo("FAIL database_url_sqlite")
        raise typer.Exit(1)
    _doctor_pass("database_url_sqlite")
    typer.echo("PASS database_url_sqlite")

    engine = create_db_engine(database_url)
    try:
        with engine.connect():
            pass
    except Exception as exc:
        _doctor_fail("database_connection", exc.__class__.__name__)
        typer.echo("FAIL database_connection")
        raise typer.Exit(1) from exc
    _doctor_pass("database_connection")
    typer.echo("PASS database_connection")

    tables = set(inspect(engine).get_table_names())
    required_tables = {"appmeta", "mediafile"}
    missing_tables = required_tables - tables
    if missing_tables:
        missing = ", ".join(sorted(missing_tables))
        _doctor_fail("required_tables", f"missing table: {missing}")
        typer.echo("FAIL required_tables")
        raise typer.Exit(1)
    _doctor_pass("required_tables")
    typer.echo("PASS required_tables")

    if not __version__:
        _doctor_fail("app_version", "missing version")
        typer.echo("FAIL app_version")
        raise typer.Exit(1)
    _doctor_pass("app_version")
    typer.echo("PASS app_version")
    requirements = _doctor_vpy_requirements(config)
    runtime_identity = build_runtime_identity(requirements)
    typer.echo(f"Image digest: {runtime_identity.avarch_image_digest}")
    typer.echo(f"Architecture: {runtime_identity.platform}")
    typer.echo(f"Python: {runtime_identity.python_version}")
    typer.echo(f"VapourSynth: {runtime_identity.vapoursynth_version}")


@app.command()
def scan(
    roots: RootsArgument = None,
) -> None:
    config = _workspace_config_path()
    app_config = _load_and_configure(config)
    database_url = resolve_database_url(app_config, config)
    _upgrade_database_or_exit(database_url)
    workspace_root = _workspace_root_for_storage(config)

    roots_to_scan = list(roots or app_config.scanner.roots)
    if not roots_to_scan:
        typer.echo("No scan roots provided or configured.")
        raise typer.Exit(1)

    engine = create_db_engine(database_url)
    results: list[ScanResult] = []
    for root in roots_to_scan:
        root = _resolve_cli_path(root)
        log.info("scan_started", root=str(root))
        try:
            snapshots = scan_root(
                root,
                extensions=app_config.scanner.extensions,
                exclude_directories=app_config.scanner.exclude_directories,
            )
            with Session(engine) as session, session.begin():
                result = update_inventory(
                    session,
                    root=root,
                    snapshots=snapshots,
                    scanned_at=_utc_now(),
                    workspace_root=workspace_root,
                )
            results.append(result)
        except ScanError as exc:
            log.error("scan_failed", root=str(root), reason=str(exc))
            typer.echo(str(exc))
            raise typer.Exit(1) from exc
        log.info("scan_completed", root=str(root))
        _echo_scan_result(result)

    if len(results) > 1:
        typer.echo("Total")
        _echo_scan_counts(
            added=sum(result.added for result in results),
            changed=sum(result.changed for result in results),
            missing=sum(result.missing for result in results),
            unchanged=sum(result.unchanged for result in results),
        )

    typer.echo("Scan complete.")


@app.command()
def files(
    changed: Annotated[
        bool,
        typer.Option("--changed", help="Show added, changed, and missing files only."),
    ] = False,
) -> None:
    config = _workspace_config_path()
    app_config = _load_and_configure(config)
    database_url = resolve_database_url(app_config, config)
    _upgrade_database_or_exit(database_url)

    engine = create_db_engine(database_url)
    with Session(engine) as session:
        statement = select(MediaFile).order_by(MediaFile.path)
        media_files = list(session.exec(statement).all())

    if changed:
        media_files = [
            media_file
            for media_file in media_files
            if _status_value(media_file.status)
            in {
                MediaFileStatus.ADDED.value,
                MediaFileStatus.CHANGED.value,
                MediaFileStatus.MISSING.value,
            }
        ]

    typer.echo("STATUS   SIZE        PATH")
    for media_file in media_files:
        typer.echo(
            f"{_status_value(media_file.status):<8} {_format_size(media_file.size_bytes):>10}  "
            f"{media_file.path}"
        )


@app.command()
def enqueue(
    profile: Annotated[str, typer.Option("--profile", help="Encoding profile name.")],
    priority: Annotated[int, typer.Option("--priority", help="Queue priority.")] = 0,
) -> None:
    config = _workspace_config_path()
    app_config = _load_and_configure(config)
    database_url = resolve_database_url(app_config, config)
    _upgrade_database_or_exit(database_url)

    runtime_config = _runtime_config(app_config, config)
    engine = create_db_engine(database_url)
    try:
        with Session(engine) as session, session.begin():
            summary = enqueue_inventory(
                session,
                config=runtime_config,
                profile_name=profile,
                priority=priority,
                now=_utc_now(),
            )
    except JobPreparationError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc

    typer.echo("Queue update")
    typer.echo("")
    typer.echo(f"Profile:         {profile}")
    typer.echo(f"Inventory files: {summary.selected}")
    typer.echo(f"Created jobs:    {summary.created}")
    typer.echo(f"Already queued:  {summary.existing}")
    typer.echo(f"Missing skipped: {summary.missing_skipped}")


@scheduler_app.command("run")
def run_queue(
    resume: Annotated[
        bool,
        typer.Option("--resume", help="Resume a paused scheduler before starting."),
    ] = False,
) -> None:
    config = _workspace_config_path()
    app_config = _load_and_configure(config)
    database_url = resolve_database_url(app_config, config)
    _upgrade_database_or_exit(database_url)
    runtime_config = _runtime_config(app_config, config)

    typer.echo("Scheduler started")
    typer.echo("")
    typer.echo("Resources:")
    typer.echo(f"  cheap workers: {runtime_config.resources.cheap_workers}")
    typer.echo(f"  Av1an jobs:    {runtime_config.resources.av1an_jobs}")
    typer.echo(f"  file ops:      {runtime_config.resources.file_ops}")
    typer.echo("")
    _echo_queue_counts(database_url)

    try:
        summary = asyncio.run(
            run_scheduler(config=runtime_config, runner_id=new_runner_id(), resume=resume)
        )
    except (SchedulerAlreadyRunningError, SchedulerControlError) as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    except KeyboardInterrupt as exc:
        raise typer.Exit(130) from exc

    typer.echo("")
    typer.echo(
        "Scheduler stopped "
        f"(completed={summary.completed}, failed={summary.failed}, skipped={summary.skipped})"
    )
    if summary.failed:
        _echo_recent_failed_jobs(database_url)


@queue_app.command("retry")
def queue_retry_command(
    status: Annotated[
        str | None,
        typer.Option("--status", help="Comma-separated job statuses."),
    ] = "failed,canceled",
    stage: Annotated[str | None, typer.Option("--stage", help="Comma-separated stages.")] = None,
    profile: Annotated[str | None, typer.Option("--profile", help="Profile name.")] = None,
    job_id: Annotated[list[int] | None, typer.Option("--job-id", help="Specific job id.")] = None,
    all_jobs: Annotated[bool, typer.Option("--all", help="Select all eligible jobs.")] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Preview without changing jobs."),
    ] = False,
    confirm: Annotated[bool, typer.Option("--confirm", help="Apply retry updates.")] = False,
) -> None:
    del dry_run
    config = _workspace_config_path()
    app_config = _load_and_configure(config)
    database_url = resolve_database_url(app_config, config)
    _upgrade_database_or_exit(database_url)
    runtime_config = _runtime_config(app_config, config)

    engine = create_db_engine(database_url)
    try:
        with Session(engine) as session, session.begin():
            summary = retry_queue(
                session,
                config=runtime_config,
                actor=cli_actor(),
                now=_utc_now(),
                job_ids=set(job_id or []) or None,
                statuses=_parse_job_statuses(status),
                stages=_parse_job_stages(stage),
                profile=profile,
                all_jobs=all_jobs,
                confirm=confirm,
            )
    except JobControlError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc

    typer.echo("Queue retry" if confirm else "Queue retry preview")
    typer.echo("")
    typer.echo(f"Matched:           {summary.matched}")
    typer.echo(f"Retryable:         {summary.retryable}")
    typer.echo(f"Requires requeue:  {summary.requires_requeue}")
    typer.echo(f"Resume probe:      {summary.reset_to_probe}")
    typer.echo(f"Resume plan:       {summary.reset_to_plan}")
    typer.echo(f"Resume encode:     {summary.reset_to_encode}")
    typer.echo(f"Resume validate:   {summary.reset_to_validate}")
    typer.echo(f"Return to promote: {summary.return_to_promote}")
    if not confirm:
        typer.echo("")
        typer.echo("No jobs were changed.")


@scheduler_app.command("pause")
def pause(
    reason: Annotated[str | None, typer.Option("--reason")] = None,
) -> None:
    config = _workspace_config_path()
    app_config = _load_and_configure(config)
    database_url = resolve_database_url(app_config, config)
    _upgrade_database_or_exit(database_url)

    engine = create_db_engine(database_url)
    try:
        with Session(engine) as session, session.begin():
            pause_scheduler(session, now=_utc_now(), reason=reason)
    except SchedulerControlError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc

    typer.echo("Scheduler pause requested")


@scheduler_app.command("resume")
def resume_scheduler_command() -> None:
    config = _workspace_config_path()
    app_config = _load_and_configure(config)
    database_url = resolve_database_url(app_config, config)
    _upgrade_database_or_exit(database_url)

    engine = create_db_engine(database_url)
    try:
        with Session(engine) as session, session.begin():
            resume_scheduler(session, now=_utc_now())
    except SchedulerControlError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    typer.echo("Scheduler resumed")


@scheduler_app.command("drain")
def drain_scheduler_command(
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    wait: Annotated[bool, typer.Option("--wait", help="Wait for the scheduler to exit.")] = False,
    timeout_seconds: Annotated[float, typer.Option("--timeout-seconds")] = 30.0,
) -> None:
    config = _workspace_config_path()
    app_config = _load_and_configure(config)
    database_url = resolve_database_url(app_config, config)
    _upgrade_database_or_exit(database_url)
    engine = create_db_engine(database_url)
    try:
        with Session(engine) as session, session.begin():
            drain_scheduler(session, now=_utc_now(), reason=reason)
    except SchedulerControlError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    typer.echo("Scheduler drain requested")
    if wait and not _wait_for_scheduler_inactive(engine, timeout_seconds=timeout_seconds):
        typer.echo("Timed out waiting for scheduler drain.")
        raise typer.Exit(1)


@scheduler_app.command("stop")
def stop_scheduler_command(
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    wait: Annotated[bool, typer.Option("--wait", help="Wait for the scheduler to exit.")] = False,
    timeout_seconds: Annotated[float, typer.Option("--timeout-seconds")] = 30.0,
) -> None:
    config = _workspace_config_path()
    app_config = _load_and_configure(config)
    database_url = resolve_database_url(app_config, config)
    _upgrade_database_or_exit(database_url)
    engine = create_db_engine(database_url)
    try:
        with Session(engine) as session, session.begin():
            stop_scheduler(session, now=_utc_now(), reason=reason)
    except SchedulerControlError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    typer.echo("Scheduler stop requested")
    if wait and not _wait_for_scheduler_inactive(engine, timeout_seconds=timeout_seconds):
        typer.echo("Timed out waiting for scheduler stop.")
        raise typer.Exit(1)


@scheduler_app.command("status")
def scheduler_status_command() -> None:
    config = _workspace_config_path()
    app_config = _load_and_configure(config)
    database_url = resolve_database_url(app_config, config)
    _upgrade_database_or_exit(database_url)
    engine = create_db_engine(database_url)
    with Session(engine) as session:
        status = scheduler_status(session, now=_utc_now())
        media_by_id = {
            media_file.id: media_file
            for media_file in session.exec(select(MediaFile)).all()
            if media_file.id is not None
        }
    typer.echo("Scheduler")
    typer.echo("")
    typer.echo(f"Mode:             {status.mode.value}")
    typer.echo(f"Control request:  {status.control_generation}")
    typer.echo(f"Acknowledged:     {status.acknowledged_generation}")
    typer.echo("")
    typer.echo(f"Runner:           {status.runner_id or 'none'}")
    typer.echo(f"Lease:            {status.lease_state}")
    typer.echo(f"Heartbeat:        {_format_relative_time(status.heartbeat_at)}")
    typer.echo(f"Expires:          {_format_expiry(status.lease_expires_at)}")
    typer.echo("")
    typer.echo("Queue:")
    for job_status in JobStatus:
        typer.echo(f"  {job_status.value:<10} {status.counts_by_status[job_status]}")
    typer.echo("")
    typer.echo("Control requests:")
    typer.echo(f"  cancel pending: {status.cancel_pending}")
    typer.echo(f"  hold pending:   {status.hold_pending}")
    if status.active_jobs:
        typer.echo("")
        typer.echo("Active:")
        for job in status.active_jobs:
            media_file = media_by_id.get(job.media_file_id)
            path = Path(media_file.path).name if media_file is not None else "<missing>"
            typer.echo(f"  {job.id:<3} {_job_stage_value(job.stage):<9} {path}")


@jobs_app.command("list")
def jobs_list(
    status: Annotated[str | None, typer.Option("--status", help="Filter by job status.")] = None,
    stage: Annotated[str | None, typer.Option("--stage", help="Filter by job stage.")] = None,
    profile: Annotated[str | None, typer.Option("--profile", help="Filter by profile.")] = None,
    limit: Annotated[int | None, typer.Option("--limit", help="Maximum rows to print.")] = None,
) -> None:
    config = _workspace_config_path()
    app_config = _load_and_configure(config)
    database_url = resolve_database_url(app_config, config)
    _upgrade_database_or_exit(database_url)

    try:
        status_filters = _parse_job_statuses(status)
        stage_filters = _parse_job_stages(stage)
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc

    engine = create_db_engine(database_url)
    with Session(engine) as session:
        statement = select(Job).order_by(
            col(Job.priority).desc(),
            col(Job.created_at).asc(),
            col(Job.id).asc(),
        )
        if status_filters is not None:
            statement = statement.where(col(Job.status).in_(status_filters))
        if stage_filters is not None:
            statement = statement.where(col(Job.stage).in_(stage_filters))
        if profile is not None:
            statement = statement.where(Job.profile_name == profile)
        if limit is not None:
            statement = statement.limit(limit)
        rows = list(session.exec(statement).all())
        media_by_id = {
            media_file.id: media_file
            for media_file in session.exec(select(MediaFile)).all()
            if media_file.id is not None
        }

    typer.echo("ID  STATUS     STAGE     PRI  TRY  CONTROL  PROFILE          FILE")
    for job in rows:
        media_file = media_by_id.get(job.media_file_id)
        path = Path(media_file.path).name if media_file is not None else "<missing>"
        control = _job_control_label(job)
        typer.echo(
            f"{job.id:<3} {_job_status_value(job.status):<10} {_job_stage_value(job.stage):<9} "
            f"{job.priority:>3}  {job.attempts:>3}  {control:<7}  {job.profile_name:<15}  {path}"
        )
        if _job_status_value(job.status) == JobStatus.FAILED.value and job.last_error_message:
            typer.echo(f"    error: {_truncate_line(job.last_error_message)}")


@jobs_app.command("show")
def jobs_show(
    job_id: Annotated[int, typer.Argument(help="Job id.")],
) -> None:
    config = _workspace_config_path()
    app_config = _load_and_configure(config)
    database_url = resolve_database_url(app_config, config)
    _upgrade_database_or_exit(database_url)

    engine = create_db_engine(database_url)
    with Session(engine) as session:
        job = session.get(Job, job_id)
        if job is None:
            typer.echo(f"Job not found: {job_id}")
            raise typer.Exit(1)
        media_file = session.get(MediaFile, job.media_file_id)
        attempts = list(
            session.exec(
                select(JobAttempt)
                .where(JobAttempt.job_id == job_id)
                .order_by(col(JobAttempt.attempt_number).asc())
            ).all()
        )
        events = list(
            session.exec(
                select(JobEvent).where(JobEvent.job_id == job_id).order_by(col(JobEvent.id).asc())
            ).all()
        )

    typer.echo(f"Job {job_id}")
    typer.echo("")
    typer.echo(f"Source:       {media_file.path if media_file is not None else '<missing>'}")
    typer.echo(f"Profile:      {job.profile_name}")
    typer.echo(f"Profile hash: {job.profile_hash}")
    typer.echo(f"Queue key:    {job.queue_key}")
    typer.echo(f"Priority:     {job.priority}")
    typer.echo(f"Status:       {_job_status_value(job.status)}")
    typer.echo(f"Stage:        {_job_stage_value(job.stage)}")
    typer.echo(f"Claimed by:   {job.claimed_by or '-'}")
    typer.echo(f"Attempts:     {job.attempts}")
    typer.echo(f"Created:      {_display_optional_datetime(job.created_at)}")
    typer.echo(f"Started:      {_display_optional_datetime(job.started_at)}")
    typer.echo(f"Finished:     {_display_optional_datetime(job.finished_at)}")
    typer.echo("")
    typer.echo("Control:")
    typer.echo(f"  cancel:     {_display_optional_datetime(job.cancel_requested_at)}")
    typer.echo(f"  hold:       {_display_optional_datetime(job.hold_requested_at)}")
    typer.echo("")
    typer.echo("Artifacts:")
    typer.echo(f"  probe hash: {job.probe_hash or '-'}")
    typer.echo(f"  plan hash:  {job.plan_hash or '-'}")
    typer.echo(f"  plan path:  {job.plan_path or '-'}")
    typer.echo(f"  output:     {job.output_path or '-'}")
    typer.echo(f"  validation: {job.latest_validation_id or '-'}")
    typer.echo(f"  promotion:  {job.latest_promotion_id or '-'}")
    if job.last_error_message:
        typer.echo("")
        typer.echo("Last error:")
        _echo_error_block(job.last_error_type, job.last_error_message, indent="  ")
    typer.echo("")
    typer.echo("Attempts:")
    for attempt in attempts:
        typer.echo(
            f"  {attempt.attempt_number:<3} {_job_stage_value(attempt.stage):<9} "
            f"{_attempt_status_value(attempt.status):<11} {attempt.runner_id}"
        )
    typer.echo("")
    typer.echo("Events:")
    for event in events:
        typer.echo(
            f"  {event.id:<3} {_display_optional_datetime(event.created_at)} "
            f"{_event_type_value(event.event_type):<16} {event.actor}"
        )


@jobs_app.command("logs")
def jobs_logs(
    job_id: Annotated[int, typer.Argument(help="Job id.")],
    attempt_number: Annotated[int | None, typer.Option("--attempt", help="Attempt number.")] = None,
    tail_bytes: Annotated[
        int,
        typer.Option("--tail-bytes", help="Bytes to tail per log."),
    ] = 16 * 1024,
) -> None:
    tail_bytes = min(tail_bytes, MAX_CLI_LOG_TAIL_BYTES)
    config = _workspace_config_path()
    app_config = _load_and_configure(config)
    database_url = resolve_database_url(app_config, config)
    _upgrade_database_or_exit(database_url)
    engine = create_db_engine(database_url)
    with Session(engine) as session:
        statement = select(JobAttempt).where(JobAttempt.job_id == job_id)
        if attempt_number is not None:
            statement = statement.where(JobAttempt.attempt_number == attempt_number)
        statement = statement.order_by(col(JobAttempt.attempt_number).desc())
        attempt = session.exec(statement).first()
    if attempt is None:
        typer.echo("No matching attempt.")
        raise typer.Exit(1)
    typer.echo(f"Attempt: {attempt.attempt_number}")
    _echo_log_tail("stdout", attempt.stdout_log, tail_bytes=tail_bytes)
    _echo_log_tail("stderr", attempt.stderr_log, tail_bytes=tail_bytes)


@jobs_app.command("cancel")
def jobs_cancel(
    job_id: Annotated[int, typer.Argument(help="Job id.")],
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    wait: Annotated[bool, typer.Option("--wait", help="Wait for cancellation to settle.")] = False,
    timeout_seconds: Annotated[float, typer.Option("--timeout-seconds")] = 30.0,
) -> None:
    config = _workspace_config_path()
    app_config = _load_and_configure(config)
    database_url = resolve_database_url(app_config, config)
    _upgrade_database_or_exit(database_url)
    engine = create_db_engine(database_url)
    try:
        with Session(engine) as session, session.begin():
            cancel_job(session, job_id=job_id, actor=cli_actor(), reason=reason, now=_utc_now())
    except JobControlError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    typer.echo("Job cancellation requested")
    if wait and not _wait_for_job_status(engine, job_id, JobStatus.CANCELED, timeout_seconds):
        typer.echo("Timed out waiting for job cancellation.")
        raise typer.Exit(1)


@jobs_app.command("hold")
def jobs_hold(
    job_id: Annotated[int, typer.Argument(help="Job id.")],
    reason: Annotated[str | None, typer.Option("--reason")] = None,
) -> None:
    config = _workspace_config_path()
    app_config = _load_and_configure(config)
    database_url = resolve_database_url(app_config, config)
    _upgrade_database_or_exit(database_url)
    engine = create_db_engine(database_url)
    try:
        with Session(engine) as session, session.begin():
            hold_job(session, job_id=job_id, actor=cli_actor(), reason=reason, now=_utc_now())
    except JobControlError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    typer.echo("Job hold requested")


@jobs_app.command("release")
def jobs_release(
    job_id: Annotated[int, typer.Argument(help="Job id.")],
) -> None:
    config = _workspace_config_path()
    app_config = _load_and_configure(config)
    database_url = resolve_database_url(app_config, config)
    _upgrade_database_or_exit(database_url)
    engine = create_db_engine(database_url)
    with Session(engine) as session, session.begin():
        changed = release_job(session, job_id=job_id, actor=cli_actor(), now=_utc_now())
    typer.echo("Job released" if changed else "No hold request exists")


@jobs_app.command("retry")
def jobs_retry(
    job_id: Annotated[int, typer.Argument(help="Job id.")],
) -> None:
    config = _workspace_config_path()
    app_config = _load_and_configure(config)
    database_url = resolve_database_url(app_config, config)
    _upgrade_database_or_exit(database_url)
    runtime_config = _runtime_config(app_config, config)
    engine = create_db_engine(database_url)
    try:
        with Session(engine) as session, session.begin():
            next_stage = retry_job(
                session,
                job_id=job_id,
                config=runtime_config,
                actor=cli_actor(),
                now=_utc_now(),
            )
    except JobControlError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    typer.echo(f"Job retry prepared at {_job_stage_value(next_stage)}")


@jobs_app.command("priority")
def jobs_priority(
    job_id: Annotated[int, typer.Argument(help="Job id.")],
    value: Annotated[int, typer.Argument(help="New priority.")],
) -> None:
    config = _workspace_config_path()
    app_config = _load_and_configure(config)
    database_url = resolve_database_url(app_config, config)
    _upgrade_database_or_exit(database_url)
    engine = create_db_engine(database_url)
    try:
        with Session(engine) as session, session.begin():
            update_job_priority(
                session,
                job_id=job_id,
                priority=value,
                actor=cli_actor(),
                now=_utc_now(),
            )
    except JobControlError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    typer.echo("Job priority updated")


@queue_app.command("clear")
def queue_clear_command(
    status: Annotated[
        str | None,
        typer.Option("--status", help="Comma-separated statuses."),
    ] = None,
    stage: Annotated[str | None, typer.Option("--stage", help="Comma-separated stages.")] = None,
    profile: Annotated[str | None, typer.Option("--profile", help="Profile name.")] = None,
    job_id: Annotated[list[int] | None, typer.Option("--job-id", help="Specific job id.")] = None,
    all_jobs: Annotated[bool, typer.Option("--all", help="Select all eligible jobs.")] = False,
    cancel_running: Annotated[bool, typer.Option("--cancel-running")] = False,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
    confirm: Annotated[bool, typer.Option("--confirm")] = False,
    wait: Annotated[bool, typer.Option("--wait")] = False,
    timeout_seconds: Annotated[float, typer.Option("--timeout-seconds")] = 30.0,
) -> None:
    del dry_run
    config = _workspace_config_path()
    app_config = _load_and_configure(config)
    database_url = resolve_database_url(app_config, config)
    _upgrade_database_or_exit(database_url)
    engine = create_db_engine(database_url)
    try:
        with Session(engine) as session, session.begin():
            summary = clear_queue(
                session,
                actor=cli_actor(),
                now=_utc_now(),
                job_ids=set(job_id or []) or None,
                statuses=_parse_job_statuses(status),
                stages=_parse_job_stages(stage),
                profile=profile,
                all_jobs=all_jobs,
                cancel_running=cancel_running,
                confirm=confirm,
            )
    except (JobControlError, ValueError) as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    typer.echo("Queue clear" if confirm else "Queue clear preview")
    typer.echo("")
    typer.echo(f"Matched:            {summary.matched}")
    typer.echo(f"Immediate cancel:   {summary.immediate_cancel}")
    typer.echo(f"Running requests:   {summary.running_requests}")
    typer.echo(f"Promotion excluded: {summary.promotion_excluded}")
    typer.echo(f"Completed excluded: {summary.completed_excluded}")
    if not confirm:
        typer.echo("")
        typer.echo("No jobs were changed.")
    if wait and not _wait_for_queue_clear(engine, timeout_seconds=timeout_seconds):
        typer.echo("Timed out waiting for running cancellations.")
        raise typer.Exit(1)


@app.command("probe")
def probe_file(
    file: Annotated[Path, typer.Argument(help="Tracked media file to probe.")],
) -> None:
    config = _workspace_config_path()
    app_config = _load_and_configure(config)
    database_url = resolve_database_url(app_config, config)
    _upgrade_database_or_exit(database_url)

    file_path = _resolve_media_path(file)
    if not file_path.exists():
        typer.echo(f"File does not exist: {file_path}")
        raise typer.Exit(1)
    if not file_path.is_file():
        typer.echo(f"Path is not a regular file: {file_path}")
        raise typer.Exit(1)

    engine = create_db_engine(database_url)
    with Session(engine) as session:
        media_file = _get_media_file(session, file_path)
        if media_file is None:
            _echo_untracked_file()
            raise typer.Exit(1)
        if _status_value(media_file.status) == MediaFileStatus.MISSING.value:
            typer.echo("File is marked missing in the media inventory.")
            typer.echo("Run avarch scan for the containing root first.")
            raise typer.Exit(1)

        try:
            raw_probe = run_ffprobe(file_path)
            normalized_probe = normalize_probe(raw_probe)
            probe_result = store_probe_result(
                session,
                media_file=media_file,
                raw_probe=raw_probe,
                normalized_probe=normalized_probe,
                created_at=_utc_now(),
            )
            session.commit()
            session.refresh(probe_result)
        except ProbeError as exc:
            session.rollback()
            typer.echo(str(exc))
            raise typer.Exit(1) from exc

    typer.echo(
        format_probe_summary(
            file_path,
            normalized_probe,
            probe_result.probe_hash,
        )
    )


@app.command("inspect")
def inspect_file(
    file: Annotated[Path, typer.Argument(help="Tracked media file to inspect.")],
) -> None:
    config = _workspace_config_path()
    app_config = _load_and_configure(config)
    database_url = resolve_database_url(app_config, config)
    _upgrade_database_or_exit(database_url)

    file_path = _resolve_media_path(file)
    engine = create_db_engine(database_url)
    with Session(engine) as session:
        media_file = _get_media_file(session, file_path)
        if media_file is None:
            _echo_untracked_file()
            raise typer.Exit(1)
        if media_file.id is None:
            typer.echo("File is not present in the media inventory.")
            raise typer.Exit(1)

        probe_result = get_canonical_probe_result(session, media_file)
        if probe_result is None:
            typer.echo("No stored probe result exists for this file.")
            raise typer.Exit(1)

        try:
            normalized_probe = parse_normalized_probe_json(probe_result.normalized_json)
        except ProbeError as exc:
            typer.echo(str(exc))
            raise typer.Exit(1) from exc

    typer.echo(format_probe_summary(file_path, normalized_probe, probe_result.probe_hash))


@app.command("validate")
def validate_file(
    output: Annotated[Path, typer.Argument(help="Planned encoded output to validate.")],
    against: Annotated[Path, typer.Option("--against", help="Source media file.")],
) -> None:
    config = _workspace_config_path()
    app_config = _load_and_configure(config)
    database_url = resolve_database_url(app_config, config)
    _upgrade_database_or_exit(database_url)
    runtime_config = _runtime_config(app_config, config)

    output_path = _resolve_media_path(output)
    source_path = _resolve_media_path(against)
    engine = create_db_engine(database_url)

    with Session(engine) as session:
        media_file = _get_media_file(session, source_path)
        if media_file is None:
            _echo_untracked_file()
            raise typer.Exit(1)
        jobs = list(
            session.exec(
                select(Job).where(
                    Job.media_file_id == media_file.id,
                    Job.output_path == str(output_path),
                    col(Job.plan_hash).is_not(None),
                )
            ).all()
        )
        if len(jobs) != 1:
            typer.echo("Unable to find exactly one planned job for this output and source.")
            raise typer.Exit(1)
        job = jobs[0]
        if job.id is None:
            typer.echo("Job is missing a database id.")
            raise typer.Exit(1)
        job_id = job.id
        if job.status == JobStatus.RUNNING:
            typer.echo("Validation cannot run while the job is running.")
            raise typer.Exit(1)
        existing = _canonical_validation(session, job)
        if (
            job.status == JobStatus.VALIDATED
            and job.stage == JobStage.PROMOTE
            and existing is not None
            and existing.passed
        ):
            report = ValidationReport.model_validate_json(existing.details_json)
            typer.echo(format_validation_report_summary(report, reused=True))
            typer.echo("")
            typer.echo("Structured report:")
            report_path = _report_path_for_job(engine, job_id)
            typer.echo(f"  {report_path if report_path is not None else '<unknown>'}")
            return
        eligible = (job.status == JobStatus.PENDING and job.stage == JobStage.VALIDATE) or (
            job.status == JobStatus.FAILED and job.stage == JobStage.VALIDATE
        )
        was_failed = job.status == JobStatus.FAILED
        if not eligible:
            typer.echo("Job is not eligible for validation.")
            raise typer.Exit(1)

    if was_failed:
        with Session(engine) as session, session.begin():
            stored = session.get(Job, job_id)
            if stored is None:
                typer.echo("Job disappeared before validation could run.")
                raise typer.Exit(1)
            stored.status = JobStatus.PENDING
            stored.stage = JobStage.VALIDATE
            stored.claimed_by = None
            stored.last_error_type = None
            stored.last_error_message = None
            stored.finished_at = None
            stored.updated_at = _utc_now()
            session.add(stored)

    result = asyncio.run(
        execute_validation_job(
            job_id=job_id,
            runner_id=new_runner_id(),
            config=runtime_config,
        )
    )
    if result is None:
        typer.echo("Validation could not run.")
        raise typer.Exit(1)

    report = ValidationReport.model_validate_json(result.details_json)
    typer.echo(format_validation_report_summary(report))
    typer.echo("")
    typer.echo("Structured report:")
    report_path = _report_path_for_job(engine, job_id)
    typer.echo(f"  {report_path if report_path is not None else '<unknown>'}")
    if not report.passed:
        raise typer.Exit(1)


@app.command("promote")
def promote_job(
    job_id: Annotated[int, typer.Argument(help="Validated job id to promote.")],
    mode: Annotated[
        PromotionMode,
        typer.Option("--mode", help="Promotion filesystem mode."),
    ] = PromotionMode.KEEP_ORIGINAL,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Preview promotion without changing files or database rows.",
        ),
    ] = False,
    confirm: Annotated[
        bool,
        typer.Option("--confirm", help="Execute the promotion transaction."),
    ] = False,
    recover: Annotated[
        bool,
        typer.Option("--recover", help="Recover an interrupted promotion for this job."),
    ] = False,
) -> None:
    config = _workspace_config_path()
    app_config = _load_and_configure(config)
    database_url = resolve_database_url(app_config, config)
    _upgrade_database_or_exit(database_url)
    runtime_config = _runtime_config(app_config, config)
    owner_token = new_runner_id()

    if recover:
        try:
            record = asyncio.run(
                recover_promotion(
                    job_id=job_id,
                    config=runtime_config,
                    owner_token=owner_token,
                )
            )
        except PromotionError as exc:
            typer.echo(str(exc))
            raise typer.Exit(1) from exc
        _echo_promotion_complete(record)
        return

    engine = create_db_engine(database_url)
    try:
        with Session(engine) as session:
            job = session.get(Job, job_id)
            if job is None:
                typer.echo(f"Job not found: {job_id}")
                raise typer.Exit(1)
            if job.plan_path is None:
                typer.echo("Job has no plan artifact.")
                raise typer.Exit(1)
            try:
                plan = TranscodePlan.model_validate_json(
                    Path(job.plan_path).read_text(encoding="utf-8")
                )
            except (OSError, ValidationError, ValueError) as exc:
                typer.echo("Job plan artifact is not usable. Regenerate the plan for this job.")
                raise typer.Exit(1) from exc
            validation = _canonical_validation(session, job)
            if validation is None:
                typer.echo("Job has no current validation result.")
                raise typer.Exit(1)
            completed = session.exec(
                select(PromotionRecord).where(
                    PromotionRecord.job_id == job_id,
                    PromotionRecord.status == PromotionStatus.COMPLETED,
                )
            ).first()
            if completed is not None:
                typer.echo("Job already has a completed promotion.")
                raise typer.Exit(1)
            preflight = validate_promotion_preflight(
                job=job,
                plan=plan,
                validation=validation,
                mode=mode,
                operation_id=new_runner_id(),
            )
    except PromotionError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc

    if dry_run or not confirm:
        _echo_promotion_preview(preflight, dry_run=dry_run)
        return

    try:
        record = asyncio.run(
            execute_promotion(
                job_id=job_id,
                mode=mode,
                config=runtime_config,
                owner_token=owner_token,
            )
        )
    except PromotionError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc

    _echo_promotion_complete(record)


@app.command("plan")
def plan_file(
    file: Annotated[Path, typer.Argument(help="Tracked media file to plan.")],
    profile: Annotated[str, typer.Option("--profile", help="Encoding profile name.")],
    check_vpy: Annotated[
        bool,
        typer.Option(
            "--check-vpy/--no-check-vpy",
            help="Run vspipe --info after writing the generated script.",
        ),
    ] = False,
) -> None:
    config = _workspace_config_path()
    app_config = _load_and_configure(config)
    database_url = resolve_database_url(app_config, config)
    _upgrade_database_or_exit(database_url)
    data_dir = resolve_data_dir(app_config, config)

    file_path = _resolve_media_path(file)
    engine = create_db_engine(database_url)
    runtime_checked = False
    try:
        registry = ProfileRegistry.from_config(app_config)
        resolved_profile = registry.get(profile)
        with Session(engine) as session:
            context = load_planning_context(
                session,
                input_path=file_path,
                resolved_profile=resolved_profile,
            )
            resolved_template = resolve_vapoursynth_template(context.profile)
            resolved_filter = resolve_vapoursynth_filter(context.profile)
            plan = build_plan(
                context,
                data_dir=data_dir,
                resolved_template=resolved_template,
                resolved_filter=resolved_filter,
            )
        vapoursynth_script = generate_vapoursynth_script(
            plan,
            template=resolved_template,
            user_filter=resolved_filter,
        )
        validate_script_syntax(vapoursynth_script)
        write_plan_artifacts(
            plan=plan,
            vapoursynth_script=vapoursynth_script,
            user_filter=resolved_filter,
            template=resolved_template,
        )
    except PlanArtifactConflictError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    except PlanningError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    except (ProfileRegistryError, UnknownProfileError) as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    except VapourSynthGenerationError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc

    if check_vpy:
        try:
            check_vapoursynth_script(
                plan.vapoursynth.script_path,
                env=runtime_environment_variables(_workspace_for_config(config)),
            )
            runtime_checked = True
        except VspipeError as exc:
            typer.echo("VapourSynth artifacts were generated, but runtime validation failed.")
            typer.echo(str(exc))
            raise typer.Exit(1) from exc

    _echo_plan_summary(plan, runtime_checked=runtime_checked, check_requested=check_vpy)


def _doctor_pass(check: str) -> None:
    log.info("doctor_check_passed", check=check)


def _doctor_fail(check: str, reason: str) -> None:
    log.error("doctor_check_failed", check=check, reason=reason)


def _upgrade_database_or_exit(database_url: str) -> None:
    try:
        upgrade_database(database_url)
    except UnsupportedDatabaseSchemaError as exc:
        log.debug("database_schema_unsupported", exc_info=exc)
        typer.echo(str(exc))
        raise typer.Exit(1) from exc


def _load_and_configure(config: Path) -> AppConfig:
    if not config.exists():
        configure_logging()
        typer.echo(f"Workspace config is missing: {config}")
        raise typer.Exit(1)

    app_config = load_config(config)
    configure_logging(app_config.logging.level, app_config.logging.format)
    log.info("config_loaded", path=str(config))

    data_dir = resolve_data_dir(app_config, config)
    data_dir.mkdir(parents=True, exist_ok=True)

    return app_config


def _runtime_config(app_config: AppConfig, config: Path) -> AppConfig:
    data_dir = resolve_data_dir(app_config, config)
    database_url = resolve_database_url(app_config, config)
    return app_config.model_copy(
        update={
            "app": app_config.app.model_copy(update={"data_dir": data_dir}),
            "database": app_config.database.model_copy(update={"url": database_url}),
        }
    )


def _primary_profile_search_path(config: AppConfig) -> Path | None:
    search_paths = config.profile_registry.search_paths
    if not search_paths:
        return None
    return search_paths[0]


def _resolve_cli_path(path: Path) -> Path:
    if path.is_absolute():
        return path

    invocation_cwd = _invocation_cwd()
    if invocation_cwd != Path.cwd():
        return invocation_cwd / path

    return path


def _invocation_cwd() -> Path:
    original_pwd = os.environ.get("PWD")
    if original_pwd:
        return Path(original_pwd)
    return Path.cwd()


def _workspace_context() -> WorkspaceContext:
    try:
        return WorkspaceContext.discover(_resolve_cli_path(Path(".")))
    except WorkspaceError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc


def _workspace_config_path() -> Path:
    return _workspace_context().config_toml


def _doctor_vpy_requirements(config: Path) -> VpyRequirements:
    workspace = WorkspaceContext(config.parent.parent.resolve())
    if workspace.vpy_requirements_toml.exists():
        return load_requirements(workspace)
    return VpyRequirements()


def _workspace_for_config(config: Path) -> WorkspaceContext:
    return WorkspaceContext(config.parent.parent.resolve())


def _workspace_root_for_storage(config: Path) -> Path | None:
    return config.parent.parent.resolve()


def _scripts_dir_for_config(config: Path) -> Path:
    return _workspace_for_config(config).scripts_dir


def _validate_vapoursynth_profile_scripts(
    profile: EncodingProfile,
    *,
    workspace: WorkspaceContext,
) -> None:
    settings = profile.vapoursynth
    if settings.mode == "generated":
        resolved_template = resolve_vapoursynth_template(profile)
        if resolved_template is not None:
            validate_script_syntax(resolved_template.text)
        return

    if settings.mode == "custom_filter":
        if settings.script is None:
            raise VapourSynthScriptPathError("custom_filter profile is missing script")
        script_path = resolve_workspace_script_path(workspace, settings.script)
        script_text = _read_required_script(script_path)
        validate_script_syntax(script_text)
        _validate_filter_entrypoint(script_text, settings.entrypoint)
        return

    if settings.template is None:
        raise VapourSynthScriptPathError("custom_template profile is missing template")
    template_path = resolve_workspace_script_path(workspace, settings.template)
    template_text = _read_required_script(template_path)
    validate_script_syntax(template_text)


def _read_required_script(path: Path) -> str:
    if not path.exists():
        raise VapourSynthScriptPathError(f"VapourSynth script does not exist: {path}")
    if not path.is_file():
        raise VapourSynthScriptPathError(f"VapourSynth script is not a regular file: {path}")
    return path.read_text(encoding="utf-8")


def _validate_filter_entrypoint(script: str, entrypoint: str) -> None:
    tree = ast.parse(script, filename="<vapoursynth-filter>")
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == entrypoint:
            if isinstance(node, ast.AsyncFunctionDef):
                raise VapourSynthScriptPathError(
                    f"VapourSynth filter entrypoint must be synchronous: {entrypoint}"
                )
            return
    raise VapourSynthScriptPathError(
        f"VapourSynth filter entrypoint was not found: {entrypoint}"
    )


def _resolve_media_path(path: Path) -> Path:
    return _resolve_cli_path(path).resolve()


def _get_media_file(session: Session, path: Path) -> MediaFile | None:
    statement = select(MediaFile).where(MediaFile.path == str(path))
    media_file = session.exec(statement).first()
    if media_file is not None:
        return media_file
    try:
        workspace = WorkspaceContext.discover(path.parent)
    except WorkspaceError:
        return None
    try:
        relative_path = str(path.resolve().relative_to(workspace.root))
    except ValueError:
        return None
    statement = select(MediaFile).where(MediaFile.path == relative_path)
    return session.exec(statement).first()


def _echo_untracked_file() -> None:
    typer.echo("File is not present in the media inventory.")
    typer.echo("Run avarch scan for the containing root first.")


def _echo_plan_summary(
    plan: TranscodePlan,
    *,
    runtime_checked: bool = False,
    check_requested: bool = False,
) -> None:
    typed_plan = plan
    subtitle_indexes = [str(stream.source_stream_index) for stream in typed_plan.subtitles.streams]
    subtitles = ", ".join(subtitle_indexes) if subtitle_indexes else "none"

    typer.echo("Plan created")
    typer.echo("")
    typer.echo(f"Input:        {typed_plan.input_path}")
    typer.echo(f"Profile:      {typed_plan.profile_name}")
    typer.echo(f"Plan hash:    {typed_plan.plan_hash}")
    typer.echo(f"Output:       {typed_plan.output_path}")
    typer.echo(f"Artifact dir: {typed_plan.artifacts.artifact_dir}")
    typer.echo("")
    typer.echo("Video:")
    typer.echo(f"  stream:     {typed_plan.video.source_stream_index}")
    typer.echo(
        "  source:     "
        f"{typed_plan.video.source_codec} "
        f"{typed_plan.video.source_width}x{typed_plan.video.source_height} "
        f"{_display_optional(typed_plan.video.source_pix_fmt)}"
    )
    typer.echo(
        f"  output:     {typed_plan.video.target_width}x{typed_plan.video.target_height} "
        f"{typed_plan.vapoursynth.output_format}"
    )
    typer.echo(f"  resize:     {'yes' if typed_plan.video.resize_required else 'no'}")
    typer.echo("")
    typer.echo(
        "Audio: "
        f"[{typed_plan.audio.source_stream_index}] "
        f"{_display_optional(typed_plan.audio.source_codec)} "
        f"{_display_optional(typed_plan.audio.source_language)} -> "
        f"{typed_plan.audio.target_codec} {typed_plan.audio.target_bitrate} "
        f"{typed_plan.audio.target_channels}ch"
    )
    typer.echo(f"Subtitles: {subtitles}")
    typer.echo("")
    typer.echo("VapourSynth:")
    typer.echo(f"  mode:       {typed_plan.vapoursynth.mode}")
    typer.echo(f"  generator:  {typed_plan.vapoursynth.generator_version}")
    typer.echo(f"  script:     {typed_plan.vapoursynth.script_path}")
    typer.echo(f"  index dir:  {typed_plan.vapoursynth.index_cache_dir}")
    typer.echo("  syntax:     PASS")
    typer.echo(
        "  runtime:    "
        f"{'PASS' if runtime_checked else 'not checked' if not check_requested else 'FAIL'}"
    )
    typer.echo("")
    typer.echo("Dry run only. No encoding was started.")


@workspace_app.command("info")
def workspace_info() -> None:
    try:
        workspace = WorkspaceContext.discover(_resolve_cli_path(Path(".")))
    except WorkspaceError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc

    typer.echo(f"Workspace: {workspace.root}")
    typer.echo(f"Config: {workspace.config_toml}")
    typer.echo(f"Database: {workspace.database_path}")
    typer.echo(f"Profiles: {workspace.profiles_dir}")
    typer.echo(f"Scripts: {workspace.scripts_dir}")
    typer.echo(f"Work: {workspace.work_dir}")


@config_app.command("show")
def config_show() -> None:
    config = _workspace_config_path()
    app_config = _load_and_configure(config)
    typer.echo(f"Config: {config}")
    typer.echo(f"Data dir: {resolve_data_dir(app_config, config)}")
    typer.echo(f"Database: {resolve_database_url(app_config, config)}")
    typer.echo("Profile search paths:")
    for search_path in app_config.profile_registry.search_paths:
        typer.echo(f"  {search_path}")


@profiles_app.command("list")
def profiles_list() -> None:
    config = _workspace_config_path()
    app_config = _load_and_configure(config)
    try:
        profiles = ProfileRegistry.from_config(app_config).list_profiles()
    except ProfileRegistryError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc

    typer.echo("NAME                 ORIGIN    SOURCE")
    for profile in profiles:
        typer.echo(f"{profile.name:<20} {profile.origin.value:<8} {profile.source}")


@profiles_app.command("copy")
def profiles_copy(
    source_name: Annotated[str, typer.Argument(help="Built-in profile to copy.")],
    name: Annotated[str, typer.Option("--name", help="New user profile name.")],
) -> None:
    config = _workspace_config_path()
    app_config = _load_and_configure(config)
    try:
        registry = ProfileRegistry.from_config(app_config)
        source = registry.get(source_name)
    except (ProfileRegistryError, UnknownProfileError) as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc

    if source.origin != ProfileOrigin.BUILTIN:
        typer.echo(f"Can only copy built-in profiles: {source_name}")
        raise typer.Exit(1)
    if name in {profile.name for profile in registry.list_profiles()}:
        typer.echo(f"Profile name is already in use or reserved: {name}")
        raise typer.Exit(1)

    destination_dir = _primary_profile_search_path(app_config)
    if destination_dir is None:
        typer.echo("No profile search path is configured.")
        raise typer.Exit(1)
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{name}.toml"
    if destination.exists():
        typer.echo(f"Profile already exists: {destination}")
        raise typer.Exit(1)

    document = source.document.model_copy(update={"name": name})
    destination.write_text(_profile_document_to_toml(document), encoding="utf-8")
    typer.echo(f"Profile created: {destination}")


@profiles_app.command("scaffold")
def profiles_scaffold(
    from_profile: Annotated[str, typer.Option("--from", help="Built-in profile to copy.")],
    name: Annotated[str, typer.Option("--name", help="New user profile name.")],
    filter_script: Annotated[
        str | None,
        typer.Option("--filter", help="Optional filter script filename."),
    ] = None,
) -> None:
    profiles_copy(source_name=from_profile, name=name)
    if filter_script is not None:
        vpy_scaffold_filter(name=Path(filter_script).stem)


@vpy_scaffold_app.command("filter")
def vpy_scaffold_filter(
    name: Annotated[str, typer.Option("--name", help="Filter module name.")],
) -> None:
    config = _workspace_config_path()
    scripts_dir = _scripts_dir_for_config(config)
    scripts_dir.mkdir(parents=True, exist_ok=True)
    destination = scripts_dir / f"{name}.py"
    if destination.exists():
        typer.echo(f"Script already exists: {destination}")
        raise typer.Exit(1)
    destination.write_text(_filter_scaffold_text(), encoding="utf-8")
    typer.echo(f"Filter scaffold created: {destination}")


@vpy_scaffold_app.command("template")
def vpy_scaffold_template(
    name: Annotated[str, typer.Option("--name", help="Template script name.")],
) -> None:
    config = _workspace_config_path()
    scripts_dir = _scripts_dir_for_config(config)
    scripts_dir.mkdir(parents=True, exist_ok=True)
    destination = scripts_dir / f"{name}.vpy"
    if destination.exists():
        typer.echo(f"Script already exists: {destination}")
        raise typer.Exit(1)
    destination.write_text(_template_scaffold_text(), encoding="utf-8")
    typer.echo(f"Template scaffold created: {destination}")


@vpy_app.command("validate")
def vpy_validate(
    profile: Annotated[str, typer.Option("--profile", help="Profile name.")],
) -> None:
    config = _workspace_config_path()
    app_config = _load_and_configure(config)
    try:
        resolved_profile = ProfileRegistry.from_config(app_config).get(profile)
        _validate_vapoursynth_profile_scripts(
            resolved_profile.profile,
            workspace=_workspace_for_config(config),
        )
    except (
        OSError,
        ProfileRegistryError,
        UnicodeDecodeError,
        UnknownProfileError,
        VapourSynthGenerationError,
    ) as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    typer.echo("Static VapourSynth validation passed")


@vpy_app.command("check")
def vpy_check(
    file: Annotated[Path, typer.Argument(help="Tracked media file to check.")],
    profile: Annotated[str, typer.Option("--profile", help="Profile name.")],
) -> None:
    config = _workspace_config_path()
    app_config = _load_and_configure(config)
    database_url = resolve_database_url(app_config, config)
    _upgrade_database_or_exit(database_url)
    data_dir = resolve_data_dir(app_config, config)
    file_path = _resolve_media_path(file)
    engine = create_db_engine(database_url)

    try:
        registry = ProfileRegistry.from_config(app_config)
        resolved_profile = registry.get(profile)
        _validate_vapoursynth_profile_scripts(
            resolved_profile.profile,
            workspace=_workspace_for_config(config),
        )
        with Session(engine) as session:
            context = load_planning_context(
                session,
                input_path=file_path,
                resolved_profile=resolved_profile,
            )
            resolved_template = resolve_vapoursynth_template(context.profile)
            resolved_filter = resolve_vapoursynth_filter(context.profile)
            plan = build_plan(
                context,
                data_dir=data_dir,
                resolved_template=resolved_template,
                resolved_filter=resolved_filter,
            )
        vapoursynth_script = generate_vapoursynth_script(
            plan,
            template=resolved_template,
            user_filter=resolved_filter,
        )
        validate_script_syntax(vapoursynth_script)
        write_plan_artifacts(
            plan=plan,
            vapoursynth_script=vapoursynth_script,
            user_filter=resolved_filter,
            template=resolved_template,
        )
        check_vapoursynth_script(
            plan.vapoursynth.script_path,
            env=runtime_environment_variables(_workspace_for_config(config)),
        )
    except (
        OSError,
        PlanArtifactConflictError,
        PlanningError,
        ProfileRegistryError,
        UnicodeDecodeError,
        UnknownProfileError,
        VapourSynthGenerationError,
        VspipeError,
    ) as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc

    typer.echo("VapourSynth runtime check passed")
    typer.echo(f"Profile: {profile}")
    typer.echo(f"Script: {plan.vapoursynth.script_path}")


@vpy_app.command("sync")
def vpy_sync() -> None:
    config = _workspace_config_path()
    workspace = _workspace_for_config(config)
    try:
        environment = sync_environment(workspace)
    except VpyEnvironmentError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    typer.echo("Workspace VapourSynth environment is current.")
    typer.echo(f"Environment: {environment.identity.environment_id}")
    typer.echo(f"Lock: {environment.lock_path}")


@vpy_app.command("plugins")
def vpy_plugins() -> None:
    try:
        plugins = list_vapoursynth_plugins()
    except VpyPluginInventoryError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc

    typer.echo("NAMESPACE            NAME                 VERSION       SOURCE     PATH")
    for plugin in plugins:
        typer.echo(
            f"{plugin.namespace:<20} {plugin.name:<20} "
            f"{(plugin.version or '-'):<13} {plugin.source:<10} {plugin.path or '-'}"
        )


@vpy_env_app.command("show")
def vpy_env_show() -> None:
    config = _workspace_config_path()
    workspace = _workspace_for_config(config)
    requirements = load_requirements(workspace)
    identity = build_runtime_identity(requirements)
    typer.echo(f"Requirements: {workspace.vpy_requirements_toml}")
    typer.echo(f"Environment:  {identity.environment_id}")
    typer.echo(f"Manifest:     {identity.manifest_hash}")
    typer.echo(f"Image digest: {identity.avarch_image_digest}")
    typer.echo(f"Python:       {identity.python_version}")
    typer.echo(f"Python ABI:   {identity.python_abi}")
    typer.echo(f"Platform:     {identity.platform}")
    typer.echo(f"VapourSynth:  {identity.vapoursynth_version}")


@vpy_env_app.command("check")
def vpy_env_check() -> None:
    config = _workspace_config_path()
    workspace = _workspace_for_config(config)
    try:
        environment = sync_environment(workspace)
    except VpyEnvironmentError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    typer.echo("PASS vpy_environment")
    typer.echo(f"Environment: {environment.identity.environment_id}")
    typer.echo(f"Lock: {environment.lock_path}")
    try:
        namespaces = ", ".join(plugin.namespace for plugin in list_vapoursynth_plugins())
    except VpyPluginInventoryError:
        namespaces = "unavailable"
    typer.echo(f"Plugin namespaces: {namespaces or 'none'}")


@vpy_packages_app.command("list")
def vpy_packages_list() -> None:
    config = _workspace_config_path()
    requirements = load_requirements(_workspace_for_config(config))
    typer.echo("Python packages:")
    for package in requirements.python.packages:
        typer.echo(f"  {package}")
    if not requirements.python.packages:
        typer.echo("  none")
    typer.echo("VSRepo packages:")
    for package in requirements.vsrepo.packages:
        typer.echo(f"  {package}")
    if not requirements.vsrepo.packages:
        typer.echo("  none")


@vpy_packages_app.command("search")
def vpy_packages_search(
    query: Annotated[str, typer.Argument(help="VSRepo package search query.")],
) -> None:
    vsrepo = shutil.which("vsrepo")
    if vsrepo is None:
        typer.echo("VSRepo is not available in this runtime.")
        raise typer.Exit(1)
    update_result = subprocess.run(
        [vsrepo, "update"],
        capture_output=True,
        text=True,
        check=False,
    )
    if update_result.returncode != 0:
        if update_result.stdout:
            typer.echo(update_result.stdout.rstrip())
        if update_result.stderr:
            typer.echo(update_result.stderr.rstrip(), err=True)
        raise typer.Exit(update_result.returncode)

    result = subprocess.run([vsrepo, "available"], capture_output=True, text=True, check=False)
    if result.stdout:
        query_folded = query.casefold()
        matches = [
            line for line in result.stdout.splitlines() if query_folded in line.casefold()
        ]
        typer.echo("\n".join(matches) if matches else "No matching VSRepo packages.")
    if result.stderr:
        typer.echo(result.stderr.rstrip(), err=True)
    raise typer.Exit(result.returncode)


@vpy_packages_app.command("install")
def vpy_packages_install(
    name: Annotated[str, typer.Argument(help="Package requirement or VSRepo package name.")],
    kind: Annotated[
        Literal["python", "vsrepo"],
        typer.Option("--kind", help="Dependency kind to add to the manifest."),
    ] = "python",
) -> None:
    config = _workspace_config_path()
    workspace = _workspace_for_config(config)
    try:
        if kind == "python":
            add_python_package(workspace, name)
        else:
            add_vsrepo_package(workspace, name)
        environment = sync_environment(workspace)
    except VpyEnvironmentError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    typer.echo(f"Package installed: {name}")
    typer.echo(f"Environment: {environment.identity.environment_id}")


@vpy_packages_app.command("remove")
def vpy_packages_remove(
    name: Annotated[str, typer.Argument(help="Package requirement or VSRepo package name.")],
    kind: Annotated[
        Literal["python", "vsrepo"],
        typer.Option("--kind", help="Dependency kind to remove from the manifest."),
    ] = "python",
) -> None:
    config = _workspace_config_path()
    workspace = _workspace_for_config(config)
    try:
        if kind == "python":
            remove_python_package(workspace, name)
        else:
            remove_vsrepo_package(workspace, name)
        environment = sync_environment(workspace)
    except VpyEnvironmentError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    typer.echo(f"Package removed: {name}")
    typer.echo(f"Environment: {environment.identity.environment_id}")


@workflow_app.command("preview")
def workflow_preview(
    profile: Annotated[str, typer.Option("--profile", help="Encoding profile name.")],
) -> None:
    config = _workspace_config_path()
    app_config = _load_and_configure(config)
    database_url = resolve_database_url(app_config, config)
    _upgrade_database_or_exit(database_url)
    engine = create_db_engine(database_url)
    with Session(engine) as session:
        count = len(session.exec(select(MediaFile)).all())
    typer.echo("Workflow preview")
    typer.echo(f"Profile: {profile}")
    typer.echo(f"Inventory files: {count}")


@workflow_app.command("enqueue")
def workflow_enqueue(
    profile: Annotated[str, typer.Option("--profile", help="Encoding profile name.")],
    priority: Annotated[int, typer.Option("--priority", help="Queue priority.")] = 0,
) -> None:
    enqueue(profile=profile, priority=priority)


@app.command("encode")
def encode_file(
    file: Annotated[Path, typer.Argument(help="Tracked media file to encode.")],
    profile: Annotated[str, typer.Option("--profile", help="Encoding profile name.")],
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Create artifacts and print commands without encoding."),
    ] = False,
    check_vpy: Annotated[
        bool,
        typer.Option(
            "--check-vpy/--no-check-vpy",
            help="Run vspipe --info after writing the generated script.",
        ),
    ] = False,
) -> None:
    config = _workspace_config_path()
    app_config = _load_and_configure(config)
    database_url = resolve_database_url(app_config, config)
    _upgrade_database_or_exit(database_url)
    data_dir = resolve_data_dir(app_config, config)

    file_path = _resolve_media_path(file)
    engine = create_db_engine(database_url)
    try:
        registry = ProfileRegistry.from_config(app_config)
        resolved_profile = registry.get(profile)
        with Session(engine) as session:
            context = load_planning_context(
                session,
                input_path=file_path,
                resolved_profile=resolved_profile,
            )
            resolved_template = resolve_vapoursynth_template(context.profile)
            resolved_filter = resolve_vapoursynth_filter(context.profile)
            plan = build_plan(
                context,
                data_dir=data_dir,
                resolved_template=resolved_template,
                resolved_filter=resolved_filter,
            )
        vapoursynth_script = generate_vapoursynth_script(
            plan,
            template=resolved_template,
            user_filter=resolved_filter,
        )
        validate_script_syntax(vapoursynth_script)
        write_plan_artifacts(
            plan=plan,
            vapoursynth_script=vapoursynth_script,
            user_filter=resolved_filter,
            template=resolved_template,
        )
        if check_vpy:
            check_vapoursynth_script(
                plan.vapoursynth.script_path,
                env=runtime_environment_variables(_workspace_for_config(config)),
            )
    except PlanArtifactConflictError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    except PlanningError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    except (ProfileRegistryError, UnknownProfileError) as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    except VapourSynthGenerationError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    except VspipeError as exc:
        typer.echo("VapourSynth artifacts were generated, but runtime validation failed.")
        typer.echo(str(exc))
        raise typer.Exit(1) from exc

    if dry_run:
        _echo_encode_dry_run(plan)
        return

    try:
        status = execute_plan(plan)
    except ExecutionError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc

    typer.echo(f"Encode {status.replace('_', ' ')}")
    typer.echo(f"Output: {plan.output_path}")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _echo_scan_result(result: ScanResult) -> None:
    typer.echo(f"Scanning {result.root}")
    _echo_scan_counts(
        added=result.added,
        changed=result.changed,
        missing=result.missing,
        unchanged=result.unchanged,
    )
    typer.echo("")


def _echo_scan_counts(*, added: int, changed: int, missing: int, unchanged: int) -> None:
    typer.echo(f"Added:     {added}")
    typer.echo(f"Changed:   {changed}")
    typer.echo(f"Missing:   {missing}")
    typer.echo(f"Unchanged: {unchanged}")


def _format_size(size_bytes: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    size = float(size_bytes)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size_bytes} B"


def _display_optional(value: str | None) -> str:
    return value if value is not None else "unknown"


def _echo_queue_counts(database_url: str) -> None:
    engine = create_db_engine(database_url)
    with Session(engine) as session:
        jobs_by_status = {
            status: len(session.exec(select(Job).where(Job.status == status)).all())
            for status in JobStatus
        }
    typer.echo("Queue:")
    typer.echo(f"  pending:   {jobs_by_status[JobStatus.PENDING]}")
    typer.echo(f"  running:   {jobs_by_status[JobStatus.RUNNING]}")
    typer.echo(f"  completed: {jobs_by_status[JobStatus.COMPLETED]}")
    typer.echo(f"  failed:    {jobs_by_status[JobStatus.FAILED]}")


def _echo_recent_failed_jobs(database_url: str, *, limit: int = 5) -> None:
    engine = create_db_engine(database_url)
    with Session(engine) as session:
        failed_jobs = list(
            session.exec(
                select(Job)
                .where(Job.status == JobStatus.FAILED)
                .order_by(col(Job.id).desc())
                .limit(limit)
            ).all()
        )
        media_by_id = {
            media_file.id: media_file
            for media_file in session.exec(select(MediaFile)).all()
            if media_file.id is not None
        }
        attempts_by_job: dict[int, JobAttempt] = {}
        if failed_jobs:
            job_ids = [job.id for job in failed_jobs if job.id is not None]
            attempts = list(
                session.exec(
                    select(JobAttempt)
                    .where(col(JobAttempt.job_id).in_(job_ids))
                    .order_by(col(JobAttempt.attempt_number).desc())
                ).all()
            )
            for attempt in attempts:
                attempts_by_job.setdefault(attempt.job_id, attempt)

    if not failed_jobs:
        return

    typer.echo("")
    typer.echo("Failed jobs:")
    for job in failed_jobs:
        media_file = media_by_id.get(job.media_file_id)
        path = Path(media_file.path).name if media_file is not None else "<missing>"
        typer.echo(f"  {job.id:<3} {_job_stage_value(job.stage):<9} {path}")
        _echo_error_block(job.last_error_type, job.last_error_message, indent="    ")
        if job.id is not None:
            attempt = attempts_by_job.get(job.id)
            if attempt is not None:
                _echo_attempt_log_status(attempt, indent="    ")


def _parse_job_statuses(value: str | None) -> set[JobStatus] | None:
    if value is None or value.strip() == "":
        return None
    statuses: set[JobStatus] = set()
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            statuses.add(JobStatus(item))
        except ValueError as exc:
            raise ValueError(f"Unknown job status: {item}") from exc
    return statuses or None


def _parse_job_stages(value: str | None) -> set[JobStage] | None:
    if value is None or value.strip() == "":
        return None
    stages: set[JobStage] = set()
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            stages.add(JobStage(item))
        except ValueError as exc:
            raise ValueError(f"Unknown job stage: {item}") from exc
    return stages or None


def _job_control_label(job: Job) -> str:
    if job.cancel_requested_at is not None and _job_status_value(job.status) != JobStatus.CANCELED:
        return "cancel"
    if job.hold_requested_at is not None:
        return "hold"
    if _job_status_value(job.status) == JobStatus.HELD.value:
        return "held"
    return "-"


def _attempt_status_value(status: object) -> str:
    return str(getattr(status, "value", status))


def _event_type_value(event_type: object) -> str:
    return str(getattr(event_type, "value", event_type))


def _display_optional_datetime(value: datetime | None) -> str:
    if value is None:
        return "-"
    return value.isoformat(sep=" ", timespec="seconds")


def _format_relative_time(value: datetime | None) -> str:
    if value is None:
        return "unknown"
    seconds = int((_utc_now().replace(tzinfo=None) - value.replace(tzinfo=None)).total_seconds())
    if seconds < 0:
        return "now"
    return f"{seconds} seconds ago"


def _format_expiry(value: datetime | None) -> str:
    if value is None:
        return "none"
    seconds = int((value.replace(tzinfo=None) - _utc_now().replace(tzinfo=None)).total_seconds())
    if seconds < 0:
        return f"{abs(seconds)} seconds ago"
    return f"in {seconds} seconds"


def _wait_for_scheduler_inactive(engine: Engine, *, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() <= deadline:
        with Session(engine) as session:
            status = scheduler_status(session, now=_utc_now())
            if status.lease_state == "inactive":
                return True
        time.sleep(0.25)
    return False


def _wait_for_job_status(
    engine: Engine,
    job_id: int,
    expected: JobStatus,
    timeout_seconds: float,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() <= deadline:
        with Session(engine) as session:
            job = session.get(Job, job_id)
            if job is not None and job.status == expected:
                return True
        time.sleep(0.25)
    return False


def _wait_for_queue_clear(engine: Engine, *, timeout_seconds: float) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() <= deadline:
        with Session(engine) as session:
            pending = session.exec(
                select(Job).where(
                    Job.status == JobStatus.RUNNING,
                    col(Job.cancel_requested_at).is_not(None),
                )
            ).first()
            if pending is None:
                return True
        time.sleep(0.25)
    return False


def _echo_log_tail(label: str, log_path: str | None, *, tail_bytes: int) -> None:
    typer.echo("")
    typer.echo(f"{label}: {log_path or 'none'}")
    if log_path is None:
        return
    path = Path(log_path)
    if not path.exists():
        typer.echo("  missing")
        return
    data = path.read_bytes()
    if len(data) > tail_bytes:
        data = b"... truncated ...\n" + data[-tail_bytes:]
    text = data.decode("utf-8", errors="replace")
    if text:
        typer.echo(text.rstrip())


def _echo_attempt_log_status(attempt: JobAttempt, *, indent: str) -> None:
    statuses: list[str] = []
    for label, log_path in (("stdout", attempt.stdout_log), ("stderr", attempt.stderr_log)):
        if log_path is None:
            statuses.append(f"{label}: -")
            continue
        path = Path(log_path)
        suffix = "" if path.exists() else " (missing)"
        statuses.append(f"{label}: {path}{suffix}")
    typer.echo(f"{indent}logs: {', '.join(statuses)}")


def _echo_error_block(
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
    for line in _truncate_text(error_message, max_length=max_length).splitlines():
        typer.echo(f"{indent}  {line}")


def _truncate_text(value: str, *, max_length: int) -> str:
    text = value.strip()
    if len(text) <= max_length:
        return text
    return f"{text[: max_length - 15].rstrip()}\n... truncated ..."


def _truncate_line(value: str, *, max_length: int = 120) -> str:
    line = " ".join(value.splitlines()).strip()
    if len(line) <= max_length:
        return line
    return f"{line[: max_length - 3]}..."


def _canonical_validation(session: Session, job: Job) -> ValidationResult | None:
    if job.latest_validation_id is None:
        return None
    result = session.get(ValidationResult, job.latest_validation_id)
    if result is None or result.job_id != job.id:
        return None
    if result.plan_hash != job.plan_hash or result.output_path != job.output_path:
        return None
    return result


def _report_path_for_job(engine: Engine, job_id: int) -> Path | None:
    with Session(engine) as session:
        job = session.get(Job, job_id)
        if job is None or job.plan_path is None:
            return None
        try:
            plan = TranscodePlan.model_validate_json(
                Path(job.plan_path).read_text(encoding="utf-8")
            )
        except (OSError, ValidationError, ValueError):
            return None
        return plan.runtime.validation_report


def _echo_encode_dry_run(plan: TranscodePlan) -> None:
    mux_temporary_path = create_mux_temporary_path(plan.output_path)
    try:
        av1an_command = build_av1an_command(plan.av1an)
        mux_command = build_ffmpeg_mux_command(plan.mux, mux_temporary_path)
    finally:
        mux_temporary_path.unlink(missing_ok=True)

    typer.echo("Encode dry run")
    typer.echo("")
    typer.echo(f"Plan hash:    {plan.plan_hash}")
    typer.echo(f"Output:       {plan.output_path}")
    typer.echo(f"Artifact dir: {plan.artifacts.artifact_dir}")
    typer.echo(f"Runtime dir:  {plan.runtime.runtime_dir}")
    typer.echo("")
    typer.echo("Av1an argv:")
    for argument in av1an_command:
        typer.echo(f"  {argument}")
    typer.echo("")
    typer.echo("FFmpeg mux argv:")
    for argument in mux_command:
        typer.echo(f"  {argument}")
    typer.echo("")
    typer.echo("No encoding was started.")


def _echo_promotion_preview(preflight: PromotionPreflightResult, *, dry_run: bool) -> None:
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


def _echo_promotion_complete(record: PromotionRecord) -> None:
    typer.echo("Promotion complete")
    typer.echo("")
    typer.echo(f"Job:               {record.job_id}")
    typer.echo(f"Mode:              {_promotion_mode_value(record.mode)}")
    typer.echo(f"Source retained:   {_source_retained_path(record)}")
    typer.echo(f"Promoted output:   {record.final_path}")
    typer.echo(f"Validation result: {record.validation_result_id}")
    typer.echo(f"Promotion record:  {record.id}")
    typer.echo(f"Cleanup:           {'complete' if record.cleanup_completed else 'warning'}")
    if record.cleanup_error:
        typer.echo(f"Cleanup warning:   {record.cleanup_error}")


def _status_value(status: MediaFileStatus | str) -> str:
    if isinstance(status, MediaFileStatus):
        return status.value
    return status


def _job_status_value(status: JobStatus | str) -> str:
    if isinstance(status, JobStatus):
        return status.value
    return status


def _job_stage_value(stage: object) -> str:
    return str(getattr(stage, "value", stage))


def _promotion_mode_value(mode: object) -> str:
    return str(getattr(mode, "value", mode))


def _source_retained_path(record: PromotionRecord) -> str:
    mode = _promotion_mode_value(record.mode)
    if mode == PromotionMode.KEEP_ORIGINAL.value:
        return record.source_path
    if record.backup_path is not None:
        return record.backup_path
    return "none"


def _profile_document_to_toml(document: ProfileDocument) -> str:
    lines = [
        "schema_version = 1",
        f'name = "{_toml_escape(document.name)}"',
    ]
    if document.description is not None:
        lines.append(f'description = "{_toml_escape(document.description)}"')
    lines.append(f"tags = {_toml_string_list(document.tags)}")
    lines.append("")
    lines.append(f"known_limitations = {_toml_string_list(document.known_limitations)}")
    lines.extend(
        (
            "",
            f'backend = "{document.backend}"',
            f'container = "{document.container}"',
            "",
            "[match]",
            f"video_codec_not = {_toml_string_list(document.match.video_codec_not)}",
            "",
            "[video]",
            f"max_width = {document.video.max_width}",
            f"hdr_to_sdr = {_toml_bool(document.video.hdr_to_sdr)}",
            f'source = "{document.video.source}"',
            "",
            "[av1an]",
            f'encoder = "{document.av1an.encoder}"',
            f"workers = {document.av1an.workers}",
            f'video_args = "{_toml_escape(document.av1an.video_args)}"',
            "",
            "[audio]",
            f'codec = "{_toml_escape(document.audio.codec)}"',
            f'bitrate = "{_toml_escape(document.audio.bitrate)}"',
            f"channels = {document.audio.channels}",
            f"languages = {_toml_string_list(document.audio.languages)}",
            "",
            "[subtitles]",
            f"languages = {_toml_string_list(document.subtitles.languages)}",
            f"keep_forced = {_toml_bool(document.subtitles.keep_forced)}",
            "",
            "[validation]",
            (
                "duration_tolerance_seconds = "
                f"{document.validation.duration_tolerance_seconds:g}"
            ),
            f"minimum_output_bytes = {document.validation.minimum_output_bytes}",
            (
                "minimum_output_source_ratio = "
                f"{document.validation.minimum_output_source_ratio:g}"
            ),
            f"decode_sample = {_toml_bool(document.validation.decode_sample)}",
            f"decode_sample_seconds = {document.validation.decode_sample_seconds:g}",
        )
    )
    if document.validation.minimum_size_reduction_percent is not None:
        lines.append(
            "minimum_size_reduction_percent = "
            f"{document.validation.minimum_size_reduction_percent:g}"
        )
    lines.append("")
    return "\n".join(lines)


def _toml_string_list(values: list[str]) -> str:
    return "[" + ", ".join(f'"{_toml_escape(value)}"' for value in values) + "]"


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"


def _filter_scaffold_text() -> str:
    return """from __future__ import annotations

import vapoursynth as vs

from avarch.vpy_api import FilterContext


def apply(video: vs.VideoNode, context: FilterContext) -> vs.VideoNode:
    del context
    return video
"""


def _template_scaffold_text() -> str:
    return """from __future__ import annotations

import vapoursynth as vs

core = vs.core

clip = core.bs.VideoSource(source=AVARCH_SOURCE_PATH)
clip = core.resize.Spline36(
    clip,
    width=AVARCH_TARGET_WIDTH,
    height=AVARCH_TARGET_HEIGHT,
    format=vs.YUV420P10,
)
clip.set_output(index=0)
"""


app.add_typer(db_app, name="db")
app.add_typer(scheduler_app, name="scheduler")
app.add_typer(jobs_app, name="jobs")
app.add_typer(queue_app, name="queue")
app.add_typer(workspace_app, name="workspace")
app.add_typer(config_app, name="config")
app.add_typer(workflow_app, name="workflow")
app.add_typer(profiles_app, name="profiles")
vpy_app.add_typer(vpy_scaffold_app, name="scaffold")
vpy_app.add_typer(vpy_env_app, name="env")
vpy_app.add_typer(vpy_packages_app, name="packages")
app.add_typer(vpy_app, name="vpy")
