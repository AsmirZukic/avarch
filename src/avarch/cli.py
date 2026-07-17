from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

import structlog
import typer
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TextColumn
from rich.table import Table
from rich.text import Text

from avarch import __version__
from avarch.application.database_admin import (
    DatabaseAdminError,
    check_database_health,
    current_database_revision,
)
from avarch.application.enqueue import PlanEnqueueSummary, enqueue_selected_plans
from avarch.application.file_views import (
    inventory_file_detail,
    list_inventory_files,
)
from avarch.application.inventory_scan import (
    InventoryScanError,
    InventoryScanResult,
    scan_inventory_root,
)
from avarch.application.job_control import (
    JobControlWorkflowError,
    cancel_jobs,
    hold_job,
    release_job,
    update_job_priority,
)
from avarch.application.job_views import (
    CurrentJobProgressView,
    WorkflowJobItem,
    current_job_progress,
    job_details,
    latest_attempt,
    list_jobs,
    recent_failed_jobs,
    workflow_jobs_for_plan_hashes,
)
from avarch.application.manual_validation import (
    prepare_validation_for_job,
    run_validation_job,
)
from avarch.application.plan_views import list_plans, plan_detail
from avarch.application.planning import PlanningError
from avarch.application.planning_workflow import (
    PlanningWorkflowItemStatus,
    create_transcode_plan_for_file,
    create_transcode_plans,
)
from avarch.application.probe_summary import (
    ProbeSummaryError,
    format_probe_summary,
    parse_normalized_probe_json,
)
from avarch.application.probing import ProbeWorkflowError, run_probe_workflow
from avarch.application.profile_management import (
    ProfileManagementError,
    copy_builtin_profile,
    list_available_profiles,
    resolve_profile,
)
from avarch.application.progress_views import JobProgressView, job_progress_view
from avarch.application.progress_watch import (
    DEFAULT_PROGRESS_WATCH_POLL_INTERVAL,
    JobProgressReader,
    JobProgressWatchInterrupted,
    JobProgressWatchNotFound,
    JobProgressWatchReadError,
    JobProgressWatchUpdate,
    watch_job_progress,
)
from avarch.application.promotion import (
    PromotionWorkflowError,
    execute_promotion,
    promotion_preflight,
    recover_promotion,
)
from avarch.application.queue_control import (
    QueueControlError,
    clear_queue,
    has_running_cancel_requests,
    retry_job,
    retry_queue,
)
from avarch.application.scheduler_control import (
    SchedulerControlWorkflowError,
    drain_scheduler,
    pause_scheduler,
    resume_scheduler,
    stop_scheduler,
)
from avarch.application.scheduler_process import (
    SchedulerLifecycleError,
    launch_detached,
    remove_metadata,
    terminate_scheduler,
    verified_status,
    workspace_lock,
    write_current_metadata,
)
from avarch.application.scheduler_run import (
    MAX_CLI_LOG_TAIL_BYTES,
    SchedulerAlreadyRunningError,
    SchedulerControlError,
    SchedulerRunSummary,
    cli_actor,
    new_runner_id,
    run_scheduler,
)
from avarch.application.scheduler_status import scheduler_status
from avarch.application.validation_summary import format_validation_report_summary
from avarch.application.vapoursynth_environment import (
    VapourSynthEnvironmentWorkflowError,
    VapourSynthPackageSearchUnavailableError,
    check_vapoursynth_environment,
    install_vapoursynth_package,
    list_vapoursynth_packages,
    list_vapoursynth_plugin_items,
    remove_vapoursynth_package,
    search_vapoursynth_packages,
    sync_vapoursynth_environment,
    vapoursynth_environment_info,
)
from avarch.application.vapoursynth_scaffolding import (
    VapourSynthScaffoldError,
    scaffold_filter_script,
    scaffold_template_script,
)
from avarch.application.vapoursynth_validation import validate_vapoursynth_profile_scripts
from avarch.application.workflow_run import verify_workflow_jobs
from avarch.application.workflow_wait import (
    wait_for_job_status,
    wait_for_queue_clear,
    wait_for_scheduler_inactive,
)
from avarch.application.workspace_management import (
    WorkspaceManagementError,
    discover_workspace_info,
    initialize_workspace,
    summarize_config,
)
from avarch.bootstrap import (
    DatabaseSchemaUpgradeError,
    PlanArtifactConflictError,
    VapourSynthEnvironmentAdapters,
    VapourSynthGenerationError,
    VpyRequirements,
    VspipeError,
    WorkspaceContext,
    WorkspaceError,
    create_workspace,
    database_admin,
    database_admin_errors,
    db_session,
    db_transaction,
    enqueue_store,
    ffprobe_collector,
    file_view_store,
    inventory_scan_workflow,
    job_control_store,
    job_view_store,
    manual_validation_preparation_store,
    manual_validation_worker,
    plan_view_store,
    planning_unit_of_work,
    probe_store,
    promotion_workflow,
    queue_control_store,
    queue_retry_store,
    scheduler_control_store,
    scheduler_process_controller,
    scheduler_runner,
    scheduler_status_store,
    upgrade_database_schema,
    vapoursynth_environment_adapters,
    vapoursynth_planning_runtime,
    workspace_database_url,
)
from avarch.cli_rendering import (
    attempt_status_value,
    display_optional,
    display_optional_datetime,
    echo_attempt_log_status,
    echo_error_block,
    echo_job_progress_details,
    echo_promotion_complete,
    echo_promotion_preview,
    event_type_value,
    format_size,
    job_control_label,
    job_outcome_summary,
    job_progress_compact_label,
    job_progress_detail_label,
    job_progress_eta_label,
    job_progress_phase_label,
    job_progress_updated_label,
    job_stage_value,
    job_status_value,
    plain_job_progress_line,
    select_progress_watch_render_mode,
    truncate_line,
)
from avarch.config import (
    AppConfig,
    load_config,
    resolve_data_dir,
)
from avarch.domain.jobs import (
    JobStage,
    JobStatus,
    ManualValidationAction,
)
from avarch.logging import configure_logging
from avarch.models.plan import TranscodePlan
from avarch.models.promotion import PromotionMode
from avarch.models.validation import ValidationReport
from avarch.adapters.sqlite.scheduler_snapshot import SqliteSchedulerSnapshotQuery
from avarch.presentation.scheduler_dashboard import (
    DashboardMode,
    render_scheduler_dashboard,
)

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CliWorkspace:
    config: Path
    app_config: AppConfig
    database_url: str
    data_dir: Path
    runtime_config: AppConfig
    workspace: WorkspaceContext
    workspace_root: Path


@dataclass(frozen=True, slots=True)
class SchedulerProgressRow:
    label: str
    view: JobProgressView


app = typer.Typer(
    name="avarch",
    help="Av1an-first archival transcoding orchestrator.",
    no_args_is_help=True,
)
db_app = typer.Typer(help="Database commands.")
scheduler_app = typer.Typer(help="Scheduler commands.")
jobs_app = typer.Typer(help="Job commands.")
queue_app = typer.Typer(help="Queue commands.")
files_app = typer.Typer(help="Inventory file commands.")
plans_app = typer.Typer(help="Plan commands.")
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
        result = initialize_workspace(
            workspace_root=workspace_root,
            force=force,
            create_workspace_func=create_workspace,
            workspace_errors=(WorkspaceError,),
        )
    except WorkspaceManagementError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc

    workspace = result.workspace
    app_config = result.config
    configure_logging(app_config.logging.level, app_config.logging.format)

    database_url = workspace_database_url(app_config, workspace.config_toml)
    _upgrade_database_or_exit(database_url)

    profiles = ", ".join(result.profile_names)

    typer.echo(f"Workspace: {workspace.root}")
    typer.echo(f"Config: {workspace.config_toml}")
    typer.echo(f"Data dir: {workspace.data_dir}")
    typer.echo(f"Profiles dir: {workspace.profiles_dir}")
    typer.echo(f"Profiles: {profiles}")


@db_app.command("upgrade")
def db_upgrade() -> None:
    cli_workspace = _load_cli_workspace()
    database_url = cli_workspace.database_url
    log.info("database_upgraded", database_url=database_url)
    typer.echo("Database upgraded")


@db_app.command("current")
def db_current() -> None:
    cli_workspace = _load_cli_workspace(upgrade_database=False)
    database_url = cli_workspace.database_url
    try:
        revision = current_database_revision(
            database_admin(database_url),
            admin_errors=database_admin_errors(),
        )
    except DatabaseAdminError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
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

    database_url = workspace_database_url(app_config, config)
    if not database_url.startswith("sqlite:///"):
        _doctor_fail("database_url_sqlite", "database URL is not SQLite")
        typer.echo("FAIL database_url_sqlite")
        raise typer.Exit(1)
    _doctor_pass("database_url_sqlite")
    typer.echo("PASS database_url_sqlite")

    try:
        health = check_database_health(
            database_admin(database_url),
            required_tables={"appmeta", "mediafile"},
            admin_errors=(Exception,),
        )
    except Exception as exc:
        _doctor_fail("database_connection", exc.__class__.__name__)
        typer.echo("FAIL database_connection")
        raise typer.Exit(1) from exc
    _doctor_pass("database_connection")
    typer.echo("PASS database_connection")

    if health.missing_tables:
        missing = ", ".join(sorted(health.missing_tables))
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
    adapters = _vapoursynth_environment_adapters()
    requirements = _doctor_vpy_requirements(config, adapters=adapters)
    runtime_identity = adapters.build_runtime_identity(requirements)
    typer.echo(f"Image digest: {runtime_identity.avarch_image_digest}")
    typer.echo(f"Architecture: {runtime_identity.platform}")
    typer.echo(f"Python: {runtime_identity.python_version}")
    typer.echo(f"VapourSynth: {runtime_identity.vapoursynth_version}")


@app.command()
def scan(
    roots: RootsArgument = None,
) -> None:
    cli_workspace = _load_cli_workspace()
    app_config = cli_workspace.app_config
    database_url = cli_workspace.database_url
    workspace_root = cli_workspace.workspace_root

    roots_to_scan = list(roots or app_config.scanner.roots)
    if not roots_to_scan:
        typer.echo("No scan roots provided or configured.")
        raise typer.Exit(1)

    scan_workflow = inventory_scan_workflow(database_url=database_url)
    results: list[InventoryScanResult] = []
    for root in roots_to_scan:
        root = _resolve_cli_path(root)
        log.info("scan_started", root=str(root))
        try:
            result = scan_inventory_root(
                scan_workflow,
                root=root,
                config=app_config,
                workspace_root=workspace_root,
                scanned_at=_utc_now(),
            )
            results.append(result)
        except InventoryScanError as exc:
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


@files_app.command("list")
def files_list(
    changed: Annotated[
        bool,
        typer.Option("--changed", help="Show added, changed, and missing files only."),
    ] = False,
) -> None:
    cli_workspace = _load_cli_workspace()
    database_url = cli_workspace.database_url

    with db_session(database_url) as session:
        media_files = list_inventory_files(file_view_store(session), changed_only=changed)

    typer.echo("STATUS   SIZE        PATH")
    for media_file in media_files:
        typer.echo(
            f"{media_file.status:<8} {format_size(media_file.size_bytes):>10}  "
            f"{media_file.path}"
        )


@files_app.command("show")
def files_show(
    file: Annotated[Path, typer.Option("--file", help="Tracked media file to inspect.")],
) -> None:
    cli_workspace = _load_cli_workspace()
    database_url = cli_workspace.database_url
    workspace_root = cli_workspace.workspace_root

    with db_session(database_url) as session:
        media_file = inventory_file_detail(
            file_view_store(session),
            file_selector=file,
            workspace_root=workspace_root,
            resolve_path=_resolve_media_path,
        )
    if media_file is None:
        _echo_untracked_file()
        raise typer.Exit(1)

    typer.echo(f"File:       {_display_media_path_value(media_file.path, workspace_root)}")
    typer.echo(f"Status:     {media_file.status}")
    typer.echo(f"Size:       {format_size(media_file.size_bytes)}")
    typer.echo(f"Fingerprint:{media_file.fs_fingerprint}")
    typer.echo(f"Probe:      {media_file.probe_hash if media_file.probe_hash is not None else '-'}")
    current_plan_hash = (
        media_file.current_plan_hash if media_file.current_plan_hash is not None else "-"
    )
    typer.echo(f"Plan:       {current_plan_hash}")
    if media_file.normalized_probe_json is not None and media_file.probe_hash is not None:
        try:
            normalized_probe = parse_normalized_probe_json(media_file.normalized_probe_json)
        except ProbeSummaryError as exc:
            typer.echo(str(exc))
            raise typer.Exit(1) from exc
        typer.echo("")
        typer.echo(
            format_probe_summary(
                _absolute_media_path_value(media_file.path, workspace_root),
                normalized_probe,
                media_file.probe_hash,
            )
        )


@app.command()
def enqueue(
    files: Annotated[
        list[Path] | None,
        typer.Option("--file", help="Restrict enqueueing to a tracked media file."),
    ] = None,
    plans: Annotated[
        list[str] | None,
        typer.Option("--plan", help="Restrict enqueueing to a persisted plan id or hash."),
    ] = None,
    priority: Annotated[int, typer.Option("--priority", help="Queue priority.")] = 0,
) -> None:
    cli_workspace = _load_cli_workspace()
    config = cli_workspace.config
    database_url = cli_workspace.database_url

    summary = _enqueue_selected_plans(
        config=config,
        database_url=database_url,
        file_selectors=files,
        plan_selectors=plans,
        priority=priority,
    )
    _echo_pipeline_summary(
        selected=summary.selected,
        processed=summary.created,
        skipped=summary.skipped,
        failed=0,
    )
    if summary.already_done:
        typer.echo(f"already-completed: {summary.already_done}")
    if summary.already_queued:
        typer.echo(f"already-queued: {summary.already_queued}")


@plans_app.command("list")
def plans_list(
    current: Annotated[
        bool,
        typer.Option("--current/--all", help="Show only current valid plans by default."),
    ] = True,
) -> None:
    cli_workspace = _load_cli_workspace()
    database_url = cli_workspace.database_url
    with db_session(database_url) as session:
        rows = list_plans(plan_view_store(session), current_only=current)

    typer.echo("ID  CURRENT  PROFILE          PLAN HASH                         FILE")
    for plan in rows:
        filename = Path(plan.media_path).name if plan.media_path is not None else "<missing>"
        current_label = "yes" if plan.current else "no"
        typer.echo(
            f"{plan.id:<3} {current_label:<8} {plan.profile_name:<15} "
            f"{plan.plan_hash[:32]:<32} {filename}"
        )


@plans_app.command("show")
def plans_show(
    plan_id: Annotated[str, typer.Argument(help="Plan id or plan hash.")],
) -> None:
    cli_workspace = _load_cli_workspace()
    database_url = cli_workspace.database_url
    with db_session(database_url) as session:
        plan = plan_detail(plan_view_store(session), selector=plan_id)
    if plan is None:
        typer.echo(f"Plan not found: {plan_id}")
        raise typer.Exit(1)

    typer.echo(f"Plan {plan.id}")
    typer.echo(f"File:       {plan.media_path if plan.media_path is not None else '<missing>'}")
    typer.echo(f"Profile:    {plan.profile_name}")
    typer.echo(f"Current:    {'yes' if plan.current else 'no'}")
    typer.echo(f"Plan hash:  {plan.plan_hash}")
    typer.echo(f"Probe hash: {plan.probe_hash}")
    typer.echo(f"Output:     {plan.output_path}")
    typer.echo(f"Artifact:   {plan.plan_path}")


@scheduler_app.command("run")
def run_queue(
    resume: Annotated[
        bool,
        typer.Option("--resume", help="Resume a paused scheduler before starting."),
    ] = False,
    detached: Annotated[
        bool,
        typer.Option("-d", "--detached", help="Run the scheduler as a detached subprocess."),
    ] = False,
    managed_child: Annotated[
        bool,
        typer.Option("--managed-child", help="Internal detached scheduler child.", hidden=True),
    ] = False,
    mode: Annotated[
        Literal["foreground", "detached"],
        typer.Option("--mode", help="Internal scheduler launch mode.", hidden=True),
    ] = "foreground",
    promote: Annotated[
        bool,
        typer.Option(
            "--promote/--no-promote",
            help="Internal promotion scheduler toggle.",
            hidden=True,
        ),
    ] = True,
) -> None:
    cli_workspace = _load_cli_workspace()
    database_url = cli_workspace.database_url
    runtime_config = cli_workspace.runtime_config
    workspace = cli_workspace.workspace

    if detached and not managed_child:
        child_argv = ["scheduler", "run"]
        if resume:
            child_argv.append("--resume")
        try:
            metadata = launch_detached(
                scheduler_process_controller(),
                workspace=workspace,
                argv=child_argv,
            )
        except SchedulerLifecycleError as exc:
            typer.echo(str(exc))
            raise typer.Exit(1) from exc
        typer.echo("Scheduler started")
        typer.echo(f"PID: {metadata.pid}")
        typer.echo(f"Log: {metadata.log_path}")
        return

    typer.echo("Scheduler started")
    typer.echo("")
    typer.echo("Resources:")
    typer.echo(f"  cheap workers: {runtime_config.resources.cheap_workers}")
    typer.echo(f"  Av1an jobs:    {runtime_config.resources.av1an_jobs}")
    typer.echo(f"  file ops:      {runtime_config.resources.file_ops}")
    typer.echo("")
    _echo_queue_counts(database_url)

    process_controller = scheduler_process_controller()
    lock = workspace_lock(process_controller, workspace=workspace)
    runner_id = new_runner_id()
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def request_stop(_signum: int, _frame: object) -> None:
        try:
            with db_transaction(database_url) as session:
                stop_scheduler(
                    scheduler_control_store(session),
                    now=_utc_now(),
                    reason="SIGTERM",
                )
        except SchedulerControlWorkflowError:
            pass

    try:
        lock.acquire()
        write_current_metadata(process_controller, workspace=workspace, mode=mode)
        signal.signal(signal.SIGTERM, request_stop)
        summary = asyncio.run(
            _run_scheduler_with_optional_live_progress(
                database_url=database_url,
                runtime_config=runtime_config,
                runner_id=runner_id,
                resume=resume,
                promote=promote,
                mode=mode,
            )
        )
    except SchedulerLifecycleError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    except (SchedulerAlreadyRunningError, SchedulerControlError) as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    except KeyboardInterrupt as exc:
        raise typer.Exit(130) from exc
    finally:
        signal.signal(signal.SIGTERM, previous_sigterm)
        remove_metadata(process_controller, workspace=workspace)
        lock.release()

    typer.echo("")
    typer.echo(
        "Scheduler stopped "
        f"(completed={summary.completed}, failed={summary.failed}, skipped={summary.skipped})"
    )
    if summary.failed:
        _echo_recent_failed_jobs(database_url)


async def _run_scheduler_with_optional_live_progress(
    *,
    database_url: str,
    runtime_config: AppConfig,
    runner_id: str,
    resume: bool,
    promote: bool,
    mode: Literal["foreground", "detached"],
) -> SchedulerRunSummary:
    runtime = scheduler_runner(
        claimable_stages=None
        if promote
        else {
            JobStage.PROBE,
            JobStage.PLAN,
            JobStage.ENCODE,
            JobStage.VALIDATE,
            JobStage.CLEANUP,
        }
    )
    scheduler_task = asyncio.create_task(
        run_scheduler(
            runtime,
            config=runtime_config,
            runner_id=runner_id,
            resume=resume,
        )
    )
    if not _scheduler_live_progress_enabled(mode=mode, stdout_is_tty=sys.stdout.isatty()):
        return await scheduler_task

    monitor_task = asyncio.create_task(_render_scheduler_live_progress(database_url))
    try:
        return await scheduler_task
    finally:
        monitor_task.cancel()
        with suppress(asyncio.CancelledError):
            await monitor_task


def _scheduler_live_progress_enabled(
    *,
    mode: Literal["foreground", "detached"],
    stdout_is_tty: bool,
) -> bool:
    return mode == "foreground" and stdout_is_tty


async def _render_scheduler_live_progress(database_url: str) -> None:
    color = os.environ.get("NO_COLOR") is None
    console = Console(file=sys.stdout, color_system="auto" if color else None)
    with Live(console=console, refresh_per_second=4, transient=False) as live:
        while True:
            live.update(_scheduler_progress_renderable(_active_scheduler_progress_rows(database_url)))
            await asyncio.sleep(DEFAULT_PROGRESS_WATCH_POLL_INTERVAL)


def _active_scheduler_progress_rows(database_url: str) -> list[SchedulerProgressRow]:
    active_statuses = {
        JobStatus.ENCODING,
        JobStatus.VALIDATING,
        JobStatus.PROMOTING,
        JobStatus.CLEANING,
    }
    now = datetime.now(UTC)
    with db_session(database_url) as session:
        store = job_view_store(session)
        jobs = list_jobs(
            store,
            statuses=active_statuses,
            stages=None,
            profile=None,
            limit=None,
        )
        rows: list[SchedulerProgressRow] = []
        for job in jobs:
            if job.id is None:
                continue
            view = job_progress_view(current_job_progress(store, job_id=job.id), now=now)
            if view is not None:
                rows.append(SchedulerProgressRow(label=job.file_name, view=view))
        return rows


def _scheduler_progress_renderable(rows: list[SchedulerProgressRow]) -> Group:
    if not rows:
        return Group(Text("Scheduler running", style="bold"), Text("Waiting for active jobs..."))
    renderables: list[RenderableType] = [Text("Scheduler running", style="bold")]
    for row in rows:
        renderables.append(_rich_progress_renderable(row.view, label=row.label))
    return Group(*renderables)


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
    cli_workspace = _load_cli_workspace()
    database_url = cli_workspace.database_url
    runtime_config = cli_workspace.runtime_config

    try:
        with db_transaction(database_url) as session:
            summary = retry_queue(
                queue_retry_store(session, config=runtime_config),
                actor=cli_actor(),
                now=_utc_now(),
                job_ids=set(job_id or []) or None,
                statuses=_parse_job_statuses(status),
                stages=_parse_job_stages(stage),
                profile=profile,
                all_jobs=all_jobs,
                confirm=confirm,
            )
    except QueueControlError as exc:
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
    cli_workspace = _load_cli_workspace()
    database_url = cli_workspace.database_url
    workspace = cli_workspace.workspace
    if verified_status(scheduler_process_controller(), workspace=workspace).metadata is None:
        typer.echo("No live scheduler process exists.")
        raise typer.Exit(1)

    try:
        with db_transaction(database_url) as session:
            pause_scheduler(
                scheduler_control_store(session),
                now=_utc_now(),
                reason=reason,
            )
    except SchedulerControlWorkflowError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc

    typer.echo("Scheduler pause requested")


@scheduler_app.command("resume")
def resume_scheduler_command() -> None:
    cli_workspace = _load_cli_workspace()
    database_url = cli_workspace.database_url
    workspace = cli_workspace.workspace
    if verified_status(scheduler_process_controller(), workspace=workspace).metadata is None:
        typer.echo("No live scheduler process exists.")
        raise typer.Exit(1)

    try:
        with db_transaction(database_url) as session:
            resume_scheduler(scheduler_control_store(session), now=_utc_now())
    except SchedulerControlWorkflowError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    typer.echo("Scheduler resumed")


@scheduler_app.command("drain")
def drain_scheduler_command(
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    wait: Annotated[bool, typer.Option("--wait", help="Wait for the scheduler to exit.")] = False,
    timeout_seconds: Annotated[float, typer.Option("--timeout-seconds")] = 30.0,
) -> None:
    cli_workspace = _load_cli_workspace()
    database_url = cli_workspace.database_url
    try:
        with db_transaction(database_url) as session:
            drain_scheduler(
                scheduler_control_store(session),
                now=_utc_now(),
                reason=reason,
            )
    except SchedulerControlWorkflowError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    typer.echo("Scheduler drain requested")
    if wait and not _wait_for_scheduler_inactive(database_url, timeout_seconds=timeout_seconds):
        typer.echo("Timed out waiting for scheduler drain.")
        raise typer.Exit(1)


@scheduler_app.command("stop")
def stop_scheduler_command(
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    wait: Annotated[bool, typer.Option("--wait", help="Wait for the scheduler to exit.")] = True,
    force: Annotated[
        bool,
        typer.Option("--force", help="Forcefully terminate the scheduler."),
    ] = False,
    timeout_seconds: Annotated[float, typer.Option("--timeout-seconds")] = 30.0,
) -> None:
    cli_workspace = _load_cli_workspace()
    database_url = cli_workspace.database_url
    workspace = cli_workspace.workspace
    live = verified_status(scheduler_process_controller(), workspace=workspace).metadata is not None
    if live and not force:
        try:
            with db_transaction(database_url) as session:
                stop_scheduler(
                    scheduler_control_store(session),
                    now=_utc_now(),
                    reason=reason,
                )
        except SchedulerControlWorkflowError as exc:
            typer.echo(str(exc))
            raise typer.Exit(1) from exc
    stopped = terminate_scheduler(
        scheduler_process_controller(),
        workspace=workspace,
        force=force,
        timeout_seconds=timeout_seconds if wait else 0.1,
    )
    typer.echo("Scheduler stopped" if stopped or not live else "Scheduler stop requested")
    if (
        wait
        and verified_status(scheduler_process_controller(), workspace=workspace).metadata
        is not None
    ):
        typer.echo("Timed out waiting for scheduler stop.")
        raise typer.Exit(1)


@scheduler_app.command("watch")
def scheduler_watch_command(
    once: Annotated[
        bool,
        typer.Option("--once", help="Print one scheduler snapshot and exit."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print one machine-readable scheduler snapshot."),
    ] = False,
    interval: Annotated[
        float,
        typer.Option("--interval", help="Polling interval for live watch mode."),
    ] = DEFAULT_PROGRESS_WATCH_POLL_INTERVAL,
    no_color: Annotated[
        bool,
        typer.Option("--no-color", help="Disable terminal colours."),
    ] = False,
) -> None:
    del interval
    if json_output:
        once = True
    if not once:
        if not sys.stdout.isatty():
            typer.echo("Live scheduler watch requires a TTY. Use --once or --json.")
            raise typer.Exit(1)
        typer.echo("Live scheduler watch is not available yet. Use --once or --json.")
        raise typer.Exit(1)

    cli_workspace = _load_cli_workspace()
    with db_session(cli_workspace.database_url) as session:
        snapshot = SqliteSchedulerSnapshotQuery(
            session,
            workspace_root=str(cli_workspace.workspace_root),
            database_url=cli_workspace.database_url,
            now=_utc_now,
        ).snapshot()

    if json_output:
        typer.echo(snapshot.to_canonical_json())
        return

    console = Console(
        file=sys.stdout,
        color_system=None if no_color else "auto",
        force_terminal=False,
    )
    console.print(
        render_scheduler_dashboard(
            snapshot,
            width=console.width,
            mode=DashboardMode.OBSERVER,
        )
    )


@scheduler_app.command("status")
def scheduler_status_command() -> None:
    cli_workspace = _load_cli_workspace()
    database_url = cli_workspace.database_url
    workspace = cli_workspace.workspace
    process_status = verified_status(scheduler_process_controller(), workspace=workspace)
    with db_session(database_url) as session:
        status = scheduler_status(scheduler_status_store(session), now=_utc_now())
    typer.echo(f"Scheduler: {process_status.state}")
    if process_status.metadata is not None:
        typer.echo(f"PID:       {process_status.metadata.pid}")
        typer.echo(f"State:     {status.mode.value}")
        typer.echo(f"Started:   {process_status.metadata.started_at}")
        typer.echo(f"Log:       {process_status.metadata.log_path}")
    elif process_status.stale_removed:
        typer.echo("State:     stale metadata removed")
    else:
        state = status.mode.value if status.lease_state != "inactive" else "stopped"
        typer.echo(f"State:     {state}")
    typer.echo(f"Lease:     {status.lease_state}")
    typer.echo(f"Runner:    {status.runner_id or 'none'}")
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
            job_id = str(job.job_id) if job.job_id is not None else "-"
            typer.echo(f"  {job_id:<3} {job_stage_value(job.stage):<9} {job.file_name}")


@scheduler_app.command("restart")
def scheduler_restart_command(
    timeout_seconds: Annotated[float, typer.Option("--timeout-seconds")] = 30.0,
) -> None:
    cli_workspace = _load_cli_workspace()
    database_url = cli_workspace.database_url
    workspace = cli_workspace.workspace
    if verified_status(scheduler_process_controller(), workspace=workspace).metadata is not None:
        try:
            with db_transaction(database_url) as session:
                stop_scheduler(
                    scheduler_control_store(session),
                    now=_utc_now(),
                    reason="restart",
                )
        except SchedulerControlWorkflowError:
            pass
        if not terminate_scheduler(
            scheduler_process_controller(),
            workspace=workspace,
            force=False,
            timeout_seconds=timeout_seconds,
        ):
            typer.echo("Unable to stop existing scheduler.")
            raise typer.Exit(1)
    try:
        metadata = launch_detached(
            scheduler_process_controller(),
            workspace=workspace,
            argv=["scheduler", "run"],
        )
    except SchedulerLifecycleError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    typer.echo("Scheduler restarted")
    typer.echo(f"PID: {metadata.pid}")
    typer.echo(f"Log: {metadata.log_path}")


@jobs_app.command("list")
def jobs_list(
    status: Annotated[str | None, typer.Option("--status", help="Filter by job status.")] = None,
    stage: Annotated[str | None, typer.Option("--stage", help="Filter by job stage.")] = None,
    profile: Annotated[str | None, typer.Option("--profile", help="Filter by profile.")] = None,
    limit: Annotated[int | None, typer.Option("--limit", help="Maximum rows to print.")] = None,
) -> None:
    cli_workspace = _load_cli_workspace()
    database_url = cli_workspace.database_url

    try:
        status_filters = _parse_job_statuses(status)
        stage_filters = _parse_job_stages(stage)
    except ValueError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc

    with db_session(database_url) as session:
        store = job_view_store(session)
        rows = list_jobs(
            store,
            statuses=status_filters,
            stages=stage_filters,
            profile=profile,
            limit=limit,
        )
        now = datetime.now(UTC)
        progress_by_job_id: dict[int, JobProgressView | None] = {}
        for job in rows:
            if job.id is not None:
                progress_by_job_id[job.id] = job_progress_view(
                    current_job_progress(store, job_id=job.id),
                    now=now,
                )

    typer.echo(
        "ID  STATUS     PHASE            PROGRESS      ETA       UPDATED   PROFILE          FILE"
    )
    for job in rows:
        path = job.file_name
        progress = progress_by_job_id.get(job.id) if job.id is not None else None
        typer.echo(
            f"{job.id:<3} {job_status_value(job.status):<10} "
            f"{job_progress_phase_label(progress):<16} "
            f"{job_progress_compact_label(progress):<13} "
            f"{job_progress_eta_label(progress):<9} "
            f"{job_progress_updated_label(progress):<9} "
            f"{job.profile_name:<15}  {path}"
        )
        control = job_control_label(job)
        if control != "—":
            typer.echo(f"    control: {control}")
        outcome = job_outcome_summary(job, path=path)
        if outcome is not None:
            typer.echo(f"    {outcome}")
        elif job_status_value(job.status) == JobStatus.FAILED.value and job.last_error_message:
            typer.echo(f"    error: {truncate_line(job.last_error_message)}")


@jobs_app.command("show")
def jobs_show(
    job_id: Annotated[int, typer.Argument(help="Job id.")],
) -> None:
    cli_workspace = _load_cli_workspace()
    database_url = cli_workspace.database_url

    with db_session(database_url) as session:
        store = job_view_store(session)
        job = job_details(store, job_id=job_id)
        if job is None:
            typer.echo(f"Job not found: {job_id}")
            raise typer.Exit(1)
        progress = job_progress_view(
            current_job_progress(store, job_id=job_id),
            now=datetime.now(UTC),
        )

    typer.echo(f"Job {job_id}")
    typer.echo("")
    typer.echo(f"Source:       {job.source_path}")
    typer.echo(f"Profile:      {job.profile_name}")
    typer.echo(f"Profile hash: {job.profile_hash}")
    typer.echo(f"Queue key:    {job.queue_key}")
    typer.echo(f"Priority:     {job.priority}")
    typer.echo(f"Status:       {job_status_value(job.status)}")
    typer.echo(f"Stage:        {job_stage_value(job.stage)}")
    typer.echo(f"Claimed by:   {job.claimed_by or '-'}")
    typer.echo(f"Attempts:     {job.attempts}")
    typer.echo(f"Created:      {display_optional_datetime(job.created_at)}")
    typer.echo(f"Started:      {display_optional_datetime(job.started_at)}")
    typer.echo(f"Finished:     {display_optional_datetime(job.finished_at)}")
    typer.echo("")
    typer.echo("Control:")
    typer.echo(f"  cancel:     {display_optional_datetime(job.cancel_requested_at)}")
    typer.echo(f"  hold:       {display_optional_datetime(job.hold_requested_at)}")
    typer.echo("")
    typer.echo("Artifacts:")
    typer.echo(f"  probe hash: {job.probe_hash or '-'}")
    typer.echo(f"  plan hash:  {job.plan_hash or '-'}")
    typer.echo(f"  plan path:  {job.plan_path or '-'}")
    typer.echo(f"  output:     {job.output_path or '-'}")
    typer.echo(f"  validation: {job.latest_validation_id or '-'}")
    typer.echo(f"  promotion:  {job.latest_promotion_id or '-'}")
    typer.echo("")
    if progress is not None:
        echo_job_progress_details(progress)
    else:
        typer.echo("Progress:")
        typer.echo("  —")
    if job.last_error_message:
        typer.echo("")
        typer.echo("Last error:")
        echo_error_block(job.last_error_type, job.last_error_message, indent="  ")
    typer.echo("")
    typer.echo("Attempts:")
    for attempt in job.attempt_history:
        typer.echo(
            f"  {attempt.attempt_number:<3} {job_stage_value(attempt.stage):<9} "
            f"{attempt_status_value(attempt.status):<11} {attempt.runner_id}"
        )
    typer.echo("")
    typer.echo("Events:")
    for event in job.events:
        typer.echo(
            f"  {event.id:<3} {display_optional_datetime(event.created_at)} "
            f"{event_type_value(event.event_type):<16} {event.actor}"
        )


@jobs_app.command("watch")
def jobs_watch(
    job_id: Annotated[int, typer.Argument(help="Job id.")],
    poll_interval: Annotated[
        float,
        typer.Option("--poll-interval", help="Seconds between progress reads."),
    ] = DEFAULT_PROGRESS_WATCH_POLL_INTERVAL,
) -> None:
    cli_workspace = _load_cli_workspace()
    database_url = cli_workspace.database_url

    with db_session(database_url) as session:
        details = job_details(job_view_store(session), job_id=job_id)
    if details is None:
        typer.echo(f"Job not found: {job_id}")
        raise typer.Exit(1)

    label = Path(details.source_path).name

    def read_current(current_job_id: int) -> CurrentJobProgressView | None:
        with db_session(database_url) as session:
            return current_job_progress(job_view_store(session), job_id=current_job_id)

    mode = select_progress_watch_render_mode(
        stdout_is_tty=sys.stdout.isatty(),
        no_color=os.environ.get("NO_COLOR"),
    )
    updates: list[JobProgressWatchUpdate] = []

    def collect(update: JobProgressWatchUpdate) -> None:
        updates.append(update)
        if not mode.live:
            typer.echo(plain_job_progress_line(update.view, label=label))

    try:
        if mode.live:
            final = _watch_job_progress_rich(
                read_current=read_current,
                job_id=job_id,
                label=label,
                poll_interval=poll_interval,
                color=mode.color,
            )
        else:
            final = watch_job_progress(
                read_current=read_current,
                job_id=job_id,
                on_update=collect,
                now=lambda: datetime.now(UTC),
                sleep=time.sleep,
                poll_interval=poll_interval,
            )
    except JobProgressWatchNotFound as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    except JobProgressWatchReadError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    except JobProgressWatchInterrupted as exc:
        raise typer.Exit(130) from exc

    raise typer.Exit(_watch_exit_code(final))


def _watch_job_progress_rich(
    *,
    read_current: JobProgressReader,
    job_id: int,
    label: str,
    poll_interval: float,
    color: bool,
) -> JobProgressView:
    console = Console(file=sys.stdout, color_system="auto" if color else None)

    def render(update: JobProgressWatchUpdate) -> None:
        live.update(_rich_progress_renderable(update.view, label=label))

    with Live(console=console, refresh_per_second=8, transient=False) as live:
        return watch_job_progress(
            read_current=read_current,
            job_id=job_id,
            on_update=render,
            now=lambda: datetime.now(UTC),
            sleep=time.sleep,
            poll_interval=poll_interval,
        )


def _rich_progress_renderable(view: JobProgressView, *, label: str) -> Group:
    title = Text(label, style="bold")
    phase = _rich_progress_phase_label(view)
    progress = Progress(
        TextColumn("{task.description}"),
        BarColumn(bar_width=None),
        TextColumn("{task.percentage:>5.1f}%"),
        expand=True,
    )
    if view.percent is None:
        progress.add_task(phase, total=None)
    else:
        progress.add_task(phase, total=1000, completed=int(view.percent * 10))
    details = Table.grid(padding=(0, 1))
    details.add_row(*_rich_progress_detail_parts(view))
    if view.message:
        details.add_row(f"stage {truncate_line(view.message, max_length=90)}")
    return Group(title, Panel.fit(Group(progress, details), border_style="cyan"))


def _rich_progress_phase_label(view: JobProgressView) -> str:
    stage = job_stage_value(view.job_stage)
    phase = job_progress_phase_label(view)
    return phase if stage == phase else f"{stage}: {phase}"


def _rich_progress_detail_parts(view: JobProgressView) -> tuple[str, ...]:
    parts = [
        job_progress_detail_label(view),
        _rich_eta_label(view),
        _rich_rate_label(view),
        _rich_speed_label(view),
        f"updated {job_progress_updated_label(view)}",
    ]
    return tuple(part for part in parts if part)


def _rich_eta_label(view: JobProgressView) -> str:
    eta = job_progress_eta_label(view)
    return "eta unknown" if eta == "—" else f"eta {eta}"


def _rich_rate_label(view: JobProgressView) -> str | None:
    if view.rate_per_second is None:
        return None
    unit = f" {view.unit.value}" if view.unit is not None else ""
    return f"rate {view.rate_per_second:.2f}{unit}/s"


def _rich_speed_label(view: JobProgressView) -> str | None:
    if view.speed_ratio is None:
        return None
    return f"speed {view.speed_ratio:.2f}x"


def _watch_exit_code(view: JobProgressView) -> int:
    if view.job_status in {JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.VALIDATION_FAILED}:
        return 1
    return 0


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
    cli_workspace = _load_cli_workspace()
    database_url = cli_workspace.database_url
    with db_session(database_url) as session:
        attempt = latest_attempt(
            job_view_store(session),
            job_id=job_id,
            attempt_number=attempt_number,
        )
    if attempt is None:
        typer.echo("No matching attempt.")
        raise typer.Exit(1)
    typer.echo(f"Attempt: {attempt.attempt_number}")
    _echo_log_tail("stdout", attempt.stdout_log, tail_bytes=tail_bytes)
    _echo_log_tail("stderr", attempt.stderr_log, tail_bytes=tail_bytes)


@jobs_app.command("cancel")
def jobs_cancel(
    job_id: Annotated[int | None, typer.Argument(help="Job id.")] = None,
    running: Annotated[bool, typer.Option("--running", help="Cancel all running jobs.")] = False,
    reason: Annotated[str | None, typer.Option("--reason")] = None,
    wait: Annotated[bool, typer.Option("--wait", help="Wait for cancellation to settle.")] = False,
    timeout_seconds: Annotated[float, typer.Option("--timeout-seconds")] = 30.0,
) -> None:
    cli_workspace = _load_cli_workspace()
    database_url = cli_workspace.database_url
    try:
        with db_transaction(database_url) as session:
            job_count = cancel_jobs(
                job_control_store(session),
                job_id=job_id,
                running=running,
                actor=cli_actor(),
                reason=reason,
                now=_utc_now(),
            )
    except JobControlWorkflowError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    typer.echo(f"Job cancellation requested: {job_count}")
    if (
        wait
        and job_id is not None
        and not _wait_for_job_status(database_url, job_id, JobStatus.CANCELLED, timeout_seconds)
    ):
        typer.echo("Timed out waiting for job cancellation.")
        raise typer.Exit(1)


@jobs_app.command("hold")
def jobs_hold(
    job_id: Annotated[int, typer.Argument(help="Job id.")],
    reason: Annotated[str | None, typer.Option("--reason")] = None,
) -> None:
    cli_workspace = _load_cli_workspace()
    database_url = cli_workspace.database_url
    try:
        with db_transaction(database_url) as session:
            hold_job(
                job_control_store(session),
                job_id=job_id,
                actor=cli_actor(),
                reason=reason,
                now=_utc_now(),
            )
    except JobControlWorkflowError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    typer.echo("Job hold requested")


@jobs_app.command("release")
def jobs_release(
    job_id: Annotated[int, typer.Argument(help="Job id.")],
) -> None:
    cli_workspace = _load_cli_workspace()
    database_url = cli_workspace.database_url
    with db_transaction(database_url) as session:
        changed = release_job(
            job_control_store(session),
            job_id=job_id,
            actor=cli_actor(),
            now=_utc_now(),
        )
    typer.echo("Job released" if changed else "No hold request exists")


@jobs_app.command("retry")
def jobs_retry(
    job_id: Annotated[int | None, typer.Argument(help="Job id.")] = None,
    failed: Annotated[bool, typer.Option("--failed", help="Retry all failed jobs.")] = False,
) -> None:
    cli_workspace = _load_cli_workspace()
    database_url = cli_workspace.database_url
    runtime_config = cli_workspace.runtime_config
    try:
        with db_transaction(database_url) as session:
            if failed:
                summary = retry_queue(
                    queue_retry_store(session, config=runtime_config),
                    actor=cli_actor(),
                    now=_utc_now(),
                    statuses={JobStatus.FAILED},
                    all_jobs=True,
                    confirm=True,
                )
                typer.echo(f"Failed jobs retry prepared: {summary.retryable}")
                return
            if job_id is None:
                typer.echo("Provide JOB_ID or --failed.")
                raise typer.Exit(1)
            next_stage = retry_job(
                queue_retry_store(session, config=runtime_config),
                job_id=job_id,
                actor=cli_actor(),
                now=_utc_now(),
            )
    except QueueControlError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    typer.echo(f"Job retry prepared at {job_stage_value(next_stage)}")


@jobs_app.command("priority")
def jobs_priority(
    job_id: Annotated[int, typer.Argument(help="Job id.")],
    value: Annotated[int, typer.Argument(help="New priority.")],
) -> None:
    cli_workspace = _load_cli_workspace()
    database_url = cli_workspace.database_url
    try:
        with db_transaction(database_url) as session:
            update_job_priority(
                job_control_store(session),
                job_id=job_id,
                priority=value,
                actor=cli_actor(),
                now=_utc_now(),
            )
    except JobControlWorkflowError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    typer.echo("Job priority updated")


@jobs_app.command("clear")
def jobs_clear(
    completed: Annotated[bool, typer.Option("--completed", help="Clear completed jobs.")] = False,
    failed: Annotated[bool, typer.Option("--failed", help="Clear failed jobs.")] = False,
    confirm: Annotated[bool, typer.Option("--confirm", help="Apply clear updates.")] = False,
) -> None:
    cli_workspace = _load_cli_workspace()
    database_url = cli_workspace.database_url
    statuses: set[JobStatus] = set()
    if completed:
        statuses.add(JobStatus.PROMOTED)
    if failed:
        statuses.add(JobStatus.FAILED)
    if not statuses:
        typer.echo("Select --completed or --failed.")
        raise typer.Exit(1)
    with db_transaction(database_url) as session:
        summary = clear_queue(
            queue_control_store(session),
            actor=cli_actor(),
            now=_utc_now(),
            statuses=statuses,
            all_jobs=True,
            confirm=confirm,
        )
    typer.echo("Jobs clear" if confirm else "Jobs clear preview")
    typer.echo(f"Matched: {summary.matched}")
    typer.echo(f"Changed: {summary.changed}")


@jobs_app.command("validate")
def jobs_validate(
    job_id: Annotated[int, typer.Argument(help="Job id.")],
) -> None:
    cli_workspace = _load_cli_workspace()
    database_url = cli_workspace.database_url
    runtime_config = cli_workspace.runtime_config
    with db_transaction(database_url) as session:
        preparation = prepare_validation_for_job(
            manual_validation_preparation_store(session),
            job_id=job_id,
            now=_utc_now(),
        )
        if preparation is None:
            typer.echo(f"Job not found: {job_id}")
            raise typer.Exit(1)
        if preparation.action == ManualValidationAction.REJECT_RUNNING:
            typer.echo("Validation cannot run while the job is running.")
            raise typer.Exit(1)
        if preparation.action == ManualValidationAction.REUSE_EXISTING:
            assert preparation.existing_validation_details_json is not None
            report = ValidationReport.model_validate_json(
                preparation.existing_validation_details_json
            )
            typer.echo(format_validation_report_summary(report, reused=True))
            return
        if preparation.action == ManualValidationAction.REJECT_INELIGIBLE:
            typer.echo("Job is not eligible for validation.")
            raise typer.Exit(1)

    result = asyncio.run(
        run_validation_job(
            manual_validation_worker(),
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
    if not report.passed:
        raise typer.Exit(1)


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
    cli_workspace = _load_cli_workspace()
    database_url = cli_workspace.database_url
    try:
        with db_transaction(database_url) as session:
            summary = clear_queue(
                queue_control_store(session),
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
    except (QueueControlError, ValueError) as exc:
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
    if wait and not _wait_for_queue_clear(database_url, timeout_seconds=timeout_seconds):
        typer.echo("Timed out waiting for running cancellations.")
        raise typer.Exit(1)


@app.command("probe")
def probe_file(
    files: Annotated[
        list[Path] | None,
        typer.Option("--file", help="Restrict probing to a tracked media file."),
    ] = None,
    force: Annotated[
        bool,
        typer.Option("--force", help="Probe selected files even if current."),
    ] = False,
) -> None:
    cli_workspace = _load_cli_workspace()
    database_url = cli_workspace.database_url
    workspace_root = cli_workspace.workspace_root

    with db_session(database_url) as session:
        try:
            result = run_probe_workflow(
                probe_store(session),
                collector=ffprobe_collector(),
                file_selectors=files,
                workspace_root=workspace_root,
                resolve_path=_resolve_media_path,
                force=force,
                now=_utc_now(),
            )
        except ProbeWorkflowError as exc:
            typer.echo(str(exc))
            typer.echo("Run avarch scan for the containing root first.")
            raise typer.Exit(1) from exc

    for probe_result in result.results:
        typer.echo(
            format_probe_summary(
                probe_result.path,
                probe_result.normalized_probe,
                probe_result.probe_hash,
            )
        )
    for message in result.messages:
        typer.echo(message)

    _echo_pipeline_summary(
        selected=result.selected,
        processed=result.processed,
        skipped=result.skipped,
        failed=result.failed,
    )
    if result.failed:
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
    cli_workspace = _load_cli_workspace()
    runtime_config = cli_workspace.runtime_config
    owner_token = new_runner_id()

    if recover:
        try:
            record = asyncio.run(
                recover_promotion(
                    promotion_workflow(),
                    job_id=job_id,
                    config=runtime_config,
                    owner_token=owner_token,
                )
            )
        except PromotionWorkflowError as exc:
            typer.echo(str(exc))
            raise typer.Exit(1) from exc
        echo_promotion_complete(record)
        return

    try:
        preflight = promotion_preflight(
            promotion_workflow(),
            job_id=job_id,
            mode=mode,
            config=runtime_config,
            operation_id=new_runner_id(),
        )
    except PromotionWorkflowError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc

    if dry_run or not confirm:
        echo_promotion_preview(preflight, dry_run=dry_run)
        return

    try:
        record = asyncio.run(
            execute_promotion(
                promotion_workflow(),
                job_id=job_id,
                mode=mode,
                config=runtime_config,
                owner_token=owner_token,
            )
        )
    except PromotionWorkflowError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc

    echo_promotion_complete(record)


@app.command("plan")
def plan_file(
    profile: Annotated[str, typer.Option("--profile", help="Encoding profile name.")],
    files: Annotated[
        list[Path] | None,
        typer.Option("--file", help="Restrict planning to a tracked media file."),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Create a new plan even when an equivalent current plan exists.",
        ),
    ] = False,
    check_vpy: Annotated[
        bool,
        typer.Option(
            "--check-vpy/--no-check-vpy",
            help="Run vspipe --info after writing the generated script.",
        ),
    ] = False,
) -> None:
    cli_workspace = _load_cli_workspace()
    app_config = cli_workspace.app_config
    database_url = cli_workspace.database_url
    data_dir = cli_workspace.data_dir
    workspace = cli_workspace.workspace
    workspace_root = cli_workspace.workspace_root
    planning_runtime = vapoursynth_planning_runtime(
        workspace,
        data_dir=data_dir,
    )

    try:
        resolved_profile = resolve_profile(app_config, profile)
    except ProfileManagementError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc

    try:
        result = create_transcode_plans(
            open_store=planning_unit_of_work(database_url),
            resolved_profile=resolved_profile,
            file_selectors=files,
            workspace_root=workspace_root,
            resolve_path=_resolve_media_path,
            data_dir=data_dir,
            runtime_identity=planning_runtime.runtime_identity,
            force=force,
            now=_utc_now(),
            generate_script=planning_runtime.generate_script,
            validate_script=planning_runtime.validate_script,
            write_artifacts=planning_runtime.write_artifacts,
            check_runtime=planning_runtime.check_runtime,
            runtime_env=planning_runtime.runtime_env,
            check_vpy=check_vpy,
            failure_errors=(
                PlanArtifactConflictError,
                PlanningError,
                VapourSynthGenerationError,
                VspipeError,
            ),
            runtime_validation_errors=(VspipeError,),
        )
    except PlanningError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc

    for item in result.items:
        display_path = _display_media_path_value(item.media_file.path, workspace_root)
        if item.status == PlanningWorkflowItemStatus.SKIPPED:
            typer.echo(f"Skipped {display_path}: {item.reason}")
            continue
        if item.status == PlanningWorkflowItemStatus.FAILED:
            if item.runtime_validation_failed:
                typer.echo("VapourSynth artifacts were generated, but runtime validation failed.")
            typer.echo(f"Failed {display_path}: {item.reason}")
            continue
        if item.plan is not None:
            _echo_plan_summary(
                item.plan,
                runtime_checked=item.runtime_checked,
                check_requested=check_vpy,
            )

    _echo_pipeline_summary(
        selected=result.selected,
        processed=result.processed,
        skipped=result.skipped,
        failed=result.failed,
    )
    if result.failed:
        raise typer.Exit(1)


@workflow_app.command("run")
def workflow_run(
    profile: Annotated[str, typer.Option("--profile", help="Encoding profile name.")],
    roots: RootsArgument = None,
    files: Annotated[
        list[Path] | None,
        typer.Option("--file", help="Restrict the workflow to a tracked media file."),
    ] = None,
    priority: Annotated[int, typer.Option("--priority", help="Queue priority.")] = 0,
    mode: Annotated[
        PromotionMode,
        typer.Option("--mode", help="Promotion filesystem mode."),
    ] = PromotionMode.KEEP_ORIGINAL,
    confirm: Annotated[
        bool,
        typer.Option("--confirm", help="Execute promotion instead of previewing it."),
    ] = False,
    force_probe: Annotated[
        bool,
        typer.Option("--force-probe", help="Probe selected files even if current."),
    ] = False,
    force_plan: Annotated[
        bool,
        typer.Option("--force-plan", help="Create new plans even if current plans exist."),
    ] = False,
    check_vpy: Annotated[
        bool,
        typer.Option(
            "--check-vpy/--no-check-vpy",
            help="Run vspipe --info after writing generated scripts.",
        ),
    ] = False,
) -> None:
    _echo_workflow_stage("scan")
    scan(roots=roots)

    _echo_workflow_stage("probe")
    probe_file(files=files, force=force_probe)

    _echo_workflow_stage("plan")
    plan_file(profile=profile, files=files, force=force_plan, check_vpy=check_vpy)

    _echo_workflow_stage("enqueue")
    cli_workspace = _load_cli_workspace()
    config = cli_workspace.config
    database_url = cli_workspace.database_url
    summary = _enqueue_selected_plans(
        config=config,
        database_url=database_url,
        file_selectors=files,
        plan_selectors=None,
        priority=priority,
    )
    _echo_pipeline_summary(
        selected=summary.selected,
        processed=summary.created,
        skipped=summary.skipped,
        failed=0,
    )
    if summary.already_done:
        typer.echo(f"already-completed: {summary.already_done}")
    if summary.already_queued:
        typer.echo(f"already-queued: {summary.already_queued}")

    if not summary.plan_hashes:
        typer.echo("No plans were selected for the workflow.")
        return

    promote_during_scheduler = confirm and mode == PromotionMode.REPLACE_ATOMIC

    _echo_workflow_stage("run")
    run_queue(
        resume=True,
        detached=False,
        managed_child=False,
        mode="foreground",
        promote=promote_during_scheduler,
    )

    _echo_workflow_stage("verify")
    jobs = _workflow_jobs_for_plan_hashes(database_url, list(summary.plan_hashes))
    readiness = verify_workflow_jobs(jobs)

    if readiness.failed_jobs:
        typer.echo("Workflow stopped before promotion because jobs failed.")
        _echo_workflow_jobs(readiness.failed_jobs)
        raise typer.Exit(1)
    if readiness.blocked_jobs:
        typer.echo("Workflow stopped before promotion because jobs are not validated yet.")
        _echo_workflow_jobs(readiness.blocked_jobs)
        raise typer.Exit(1)
    if not readiness.promotable_jobs:
        if promote_during_scheduler:
            typer.echo("Promotion completed by scheduler.")
            return
        typer.echo("No validated jobs are ready for promotion.")
        return
    typer.echo(f"Validated jobs ready for promotion: {len(readiness.promotable_jobs)}")

    _echo_workflow_stage("promote")
    for job in readiness.promotable_jobs:
        if job.id is None:
            continue
        promote_job(
            job_id=job.id,
            mode=mode,
            dry_run=not confirm,
            confirm=confirm,
            recover=False,
        )


def _doctor_pass(check: str) -> None:
    log.info("doctor_check_passed", check=check)


def _doctor_fail(check: str, reason: str) -> None:
    log.error("doctor_check_failed", check=check, reason=reason)


def _upgrade_database_or_exit(database_url: str) -> None:
    try:
        upgrade_database_schema(database_url)
    except DatabaseSchemaUpgradeError as exc:
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
    log.debug("config_loaded", path=str(config))

    data_dir = resolve_data_dir(app_config, config)
    data_dir.mkdir(parents=True, exist_ok=True)

    return app_config


def _load_cli_workspace(*, upgrade_database: bool = True) -> CliWorkspace:
    config = _workspace_config_path()
    app_config = _load_and_configure(config)
    database_url = workspace_database_url(app_config, config)
    if upgrade_database:
        _upgrade_database_or_exit(database_url)
    data_dir = resolve_data_dir(app_config, config)
    runtime_config = _runtime_config(
        app_config,
        config,
        data_dir=data_dir,
        database_url=database_url,
    )
    workspace = _workspace_for_config(config)
    workspace_root = _workspace_root_for_storage(config) or Path.cwd()
    return CliWorkspace(
        config=config,
        app_config=app_config,
        database_url=database_url,
        data_dir=data_dir,
        runtime_config=runtime_config,
        workspace=workspace,
        workspace_root=workspace_root,
    )


def _runtime_config(
    app_config: AppConfig,
    config: Path,
    *,
    data_dir: Path | None = None,
    database_url: str | None = None,
) -> AppConfig:
    data_dir = data_dir or resolve_data_dir(app_config, config)
    database_url = database_url or workspace_database_url(app_config, config)
    return app_config.model_copy(
        update={
            "app": app_config.app.model_copy(update={"data_dir": data_dir}),
            "database": app_config.database.model_copy(update={"url": database_url}),
        }
    )


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


def _doctor_vpy_requirements(
    config: Path,
    *,
    adapters: VapourSynthEnvironmentAdapters,
) -> VpyRequirements:
    workspace = WorkspaceContext(config.parent.parent.resolve())
    if workspace.vpy_requirements_toml.exists():
        return adapters.load_requirements(workspace)
    return VpyRequirements()


def _vapoursynth_environment_adapters() -> VapourSynthEnvironmentAdapters:
    return vapoursynth_environment_adapters()


def _workspace_for_config(config: Path) -> WorkspaceContext:
    return WorkspaceContext(config.parent.parent.resolve())


def _workspace_root_for_storage(config: Path) -> Path | None:
    return config.parent.parent.resolve()


def _resolve_media_path(path: Path) -> Path:
    return _resolve_cli_path(path).resolve()


def _absolute_media_path_value(path_value: str, workspace_root: Path) -> Path:
    path = Path(path_value)
    if path.is_absolute():
        return path
    return workspace_root / path


def _display_media_path_value(path_value: str, workspace_root: Path) -> str:
    return str(_absolute_media_path_value(path_value, workspace_root))


def _enqueue_selected_plans(
    *,
    config: Path,
    database_url: str,
    file_selectors: list[Path] | None,
    plan_selectors: list[str] | None,
    priority: int,
) -> PlanEnqueueSummary:
    workspace_root = _workspace_root_for_storage(config) or Path.cwd()
    with db_transaction(database_url) as session:
        try:
            return enqueue_selected_plans(
                enqueue_store(session),
                file_selectors=file_selectors,
                plan_selectors=plan_selectors,
                workspace_root=workspace_root,
                resolve_path=_resolve_media_path,
                priority=priority,
                now=_utc_now(),
            )
        except ValueError as exc:
            typer.echo(str(exc))
            raise typer.Exit(1) from exc


def _echo_pipeline_summary(
    *,
    selected: int,
    processed: int,
    skipped: int,
    failed: int,
) -> None:
    typer.echo("Summary")
    typer.echo(f"Selected: {selected}")
    typer.echo(f"Processed: {processed}")
    typer.echo(f"Skipped: {skipped}")
    typer.echo(f"Failed: {failed}")


def _echo_workflow_stage(name: str) -> None:
    typer.echo("")
    typer.echo(f"== {name} ==")


def _workflow_jobs_for_plan_hashes(
    database_url: str,
    plan_hashes: list[str],
) -> list[WorkflowJobItem]:
    if not plan_hashes:
        return []
    with db_session(database_url) as session:
        return workflow_jobs_for_plan_hashes(
            job_view_store(session),
            plan_hashes=tuple(plan_hashes),
        )


def _echo_workflow_jobs(jobs: Sequence[WorkflowJobItem]) -> None:
    for job in jobs:
        typer.echo(
            f"  {job.id or '-'} {job_status_value(job.status):<10} "
            f"{job_stage_value(job.stage):<9} {job.output_path or job.plan_hash or '-'}"
        )


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
        f"{display_optional(typed_plan.video.source_pix_fmt)}"
    )
    typer.echo(
        f"  output:     {typed_plan.video.target_width}x{typed_plan.video.target_height} "
        f"{typed_plan.vapoursynth.output_format}"
    )
    typer.echo(f"  resize:     {'yes' if typed_plan.video.resize_required else 'no'}")
    typer.echo("")
    if typed_plan.audio is None:
        typer.echo("Audio: none")
    else:
        typer.echo(
            "Audio: "
            f"[{typed_plan.audio.source_stream_index}] "
            f"{display_optional(typed_plan.audio.source_codec)} "
            f"{display_optional(typed_plan.audio.source_language)} -> "
            f"{typed_plan.audio.target_codec} {typed_plan.audio.target_bitrate} "
            f"{typed_plan.audio.target_channels}ch"
        )
    typer.echo(f"Subtitles: {subtitles}")
    typer.echo("")
    typer.echo("Av1an:")
    typer.echo(f"  workers:   {typed_plan.av1an.workers}")
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
        workspace = discover_workspace_info(
            _resolve_cli_path(Path(".")),
            discover_workspace_func=WorkspaceContext.discover,
            workspace_errors=(WorkspaceError,),
        )
    except WorkspaceManagementError as exc:
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
    cli_workspace = _load_cli_workspace(upgrade_database=False)
    summary = summarize_config(
        config_path=cli_workspace.config,
        config=cli_workspace.app_config,
        database_url=cli_workspace.database_url,
    )
    typer.echo(f"Config: {summary.config_path}")
    typer.echo(f"Data dir: {summary.data_dir}")
    typer.echo(f"Database: {summary.database_url}")
    typer.echo("Profile search paths:")
    for search_path in summary.profile_search_paths:
        typer.echo(f"  {search_path}")


@profiles_app.command("list")
def profiles_list() -> None:
    cli_workspace = _load_cli_workspace(upgrade_database=False)
    try:
        profiles = list_available_profiles(cli_workspace.app_config)
    except ProfileManagementError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc

    typer.echo("NAME                 ORIGIN    SOURCE")
    for profile in profiles:
        typer.echo(f"{profile.name:<20} {profile.origin:<8} {profile.source}")


@profiles_app.command("copy")
def profiles_copy(
    source_name: Annotated[str, typer.Argument(help="Built-in profile to copy.")],
    name: Annotated[str, typer.Option("--name", help="New user profile name.")],
) -> None:
    cli_workspace = _load_cli_workspace(upgrade_database=False)
    try:
        result = copy_builtin_profile(
            cli_workspace.app_config,
            source_name=source_name,
            name=name,
        )
    except ProfileManagementError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    typer.echo(f"Profile created: {result.destination}")


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
    cli_workspace = _load_cli_workspace(upgrade_database=False)
    try:
        result = scaffold_filter_script(
            scripts_dir=cli_workspace.workspace.scripts_dir,
            name=name,
        )
    except VapourSynthScaffoldError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    typer.echo(f"Filter scaffold created: {result.destination}")


@vpy_scaffold_app.command("template")
def vpy_scaffold_template(
    name: Annotated[str, typer.Option("--name", help="Template script name.")],
) -> None:
    cli_workspace = _load_cli_workspace(upgrade_database=False)
    try:
        result = scaffold_template_script(
            scripts_dir=cli_workspace.workspace.scripts_dir,
            name=name,
        )
    except VapourSynthScaffoldError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    typer.echo(f"Template scaffold created: {result.destination}")


@vpy_app.command("validate")
def vpy_validate(
    profile: Annotated[str, typer.Option("--profile", help="Profile name.")],
) -> None:
    cli_workspace = _load_cli_workspace(upgrade_database=False)
    try:
        resolved_profile = resolve_profile(cli_workspace.app_config, profile)
        validate_vapoursynth_profile_scripts(
            resolved_profile.profile,
            workspace=cli_workspace.workspace,
            validate_script=vapoursynth_planning_runtime(
                cli_workspace.workspace,
                data_dir=cli_workspace.data_dir,
            ).validate_script,
        )
    except (
        OSError,
        UnicodeDecodeError,
        ProfileManagementError,
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
    cli_workspace = _load_cli_workspace()
    app_config = cli_workspace.app_config
    database_url = cli_workspace.database_url
    data_dir = cli_workspace.data_dir
    workspace = cli_workspace.workspace
    planning_runtime = vapoursynth_planning_runtime(
        workspace,
        data_dir=data_dir,
    )

    try:
        resolved_profile = resolve_profile(app_config, profile)
        validate_vapoursynth_profile_scripts(
            resolved_profile.profile,
            workspace=workspace,
            validate_script=planning_runtime.validate_script,
        )
        materialized = create_transcode_plan_for_file(
            open_store=planning_unit_of_work(database_url),
            resolved_profile=resolved_profile,
            input_path=_resolve_media_path(file),
            data_dir=data_dir,
            runtime_identity=planning_runtime.runtime_identity,
            generate_script=planning_runtime.generate_script,
            validate_script=planning_runtime.validate_script,
            write_artifacts=planning_runtime.write_artifacts,
            check_runtime=planning_runtime.check_runtime,
            runtime_env=planning_runtime.runtime_env,
            check_vpy=True,
        )
        plan = materialized.plan
    except (
        OSError,
        PlanArtifactConflictError,
        PlanningError,
        UnicodeDecodeError,
        ProfileManagementError,
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
    cli_workspace = _load_cli_workspace(upgrade_database=False)
    workspace = cli_workspace.workspace
    adapters = _vapoursynth_environment_adapters()
    try:
        environment = sync_vapoursynth_environment(
            workspace,
            sync_environment_func=adapters.sync_environment,
            environment_errors=adapters.environment_errors,
        )
    except VapourSynthEnvironmentWorkflowError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    typer.echo("Workspace VapourSynth environment is current.")
    typer.echo(f"Environment: {environment.environment_id}")
    typer.echo(f"Lock: {environment.lock_path}")


@vpy_app.command("plugins")
def vpy_plugins() -> None:
    adapters = _vapoursynth_environment_adapters()
    try:
        plugins = list_vapoursynth_plugin_items(
            list_plugins_func=adapters.list_plugins,
            plugin_inventory_errors=adapters.plugin_inventory_errors,
        )
    except VapourSynthEnvironmentWorkflowError as exc:
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
    cli_workspace = _load_cli_workspace(upgrade_database=False)
    workspace = cli_workspace.workspace
    adapters = _vapoursynth_environment_adapters()
    info = vapoursynth_environment_info(
        workspace,
        load_requirements_func=adapters.load_requirements,
        build_runtime_identity_func=adapters.build_runtime_identity,
        environment_errors=adapters.environment_errors,
    )
    typer.echo(f"Requirements: {info.requirements_path}")
    typer.echo(f"Environment:  {info.environment_id}")
    typer.echo(f"Manifest:     {info.manifest_hash}")
    typer.echo(f"Image digest: {info.avarch_image_digest}")
    typer.echo(f"Python:       {info.python_version}")
    typer.echo(f"Python ABI:   {info.python_abi}")
    typer.echo(f"Platform:     {info.platform}")
    typer.echo(f"VapourSynth:  {info.vapoursynth_version}")


@vpy_env_app.command("check")
def vpy_env_check() -> None:
    cli_workspace = _load_cli_workspace(upgrade_database=False)
    workspace = cli_workspace.workspace
    adapters = _vapoursynth_environment_adapters()
    try:
        result = check_vapoursynth_environment(
            workspace,
            sync_environment_func=adapters.sync_environment,
            list_plugins_func=adapters.list_plugins,
            environment_errors=adapters.environment_errors,
            plugin_inventory_errors=adapters.plugin_inventory_errors,
        )
    except VapourSynthEnvironmentWorkflowError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    typer.echo("PASS vpy_environment")
    typer.echo(f"Environment: {result.environment_id}")
    typer.echo(f"Lock: {result.lock_path}")
    typer.echo(f"Plugin namespaces: {result.plugin_namespaces}")


@vpy_packages_app.command("list")
def vpy_packages_list() -> None:
    cli_workspace = _load_cli_workspace(upgrade_database=False)
    adapters = _vapoursynth_environment_adapters()
    packages = list_vapoursynth_packages(
        cli_workspace.workspace,
        load_requirements_func=adapters.load_requirements,
        environment_errors=adapters.environment_errors,
    )
    typer.echo("Python packages:")
    for package in packages.python_packages:
        typer.echo(f"  {package}")
    if not packages.python_packages:
        typer.echo("  none")
    typer.echo("VSRepo packages:")
    for package in packages.vsrepo_packages:
        typer.echo(f"  {package}")
    if not packages.vsrepo_packages:
        typer.echo("  none")


@vpy_packages_app.command("search")
def vpy_packages_search(
    query: Annotated[str, typer.Argument(help="VSRepo package search query.")],
) -> None:
    adapters = _vapoursynth_environment_adapters()
    try:
        result = search_vapoursynth_packages(
            query,
            search_func=adapters.search_packages,
            unavailable_errors=adapters.package_search_unavailable_errors,
        )
    except VapourSynthPackageSearchUnavailableError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    if result.matches is None:
        if result.stdout:
            typer.echo(result.stdout.rstrip())
        if result.stderr:
            typer.echo(result.stderr.rstrip(), err=True)
        raise typer.Exit(result.returncode)
    typer.echo("\n".join(result.matches) if result.matches else "No matching VSRepo packages.")
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
    cli_workspace = _load_cli_workspace(upgrade_database=False)
    workspace = cli_workspace.workspace
    adapters = _vapoursynth_environment_adapters()
    try:
        environment = install_vapoursynth_package(
            workspace,
            name=name,
            kind=kind,
            add_python_package_func=adapters.add_python_package,
            add_vsrepo_package_func=adapters.add_vsrepo_package,
            sync_environment_func=adapters.sync_environment,
            environment_errors=adapters.environment_errors,
        )
    except VapourSynthEnvironmentWorkflowError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    typer.echo(f"Package installed: {name}")
    typer.echo(f"Environment: {environment.environment_id}")


@vpy_packages_app.command("remove")
def vpy_packages_remove(
    name: Annotated[str, typer.Argument(help="Package requirement or VSRepo package name.")],
    kind: Annotated[
        Literal["python", "vsrepo"],
        typer.Option("--kind", help="Dependency kind to remove from the manifest."),
    ] = "python",
) -> None:
    cli_workspace = _load_cli_workspace(upgrade_database=False)
    workspace = cli_workspace.workspace
    adapters = _vapoursynth_environment_adapters()
    try:
        environment = remove_vapoursynth_package(
            workspace,
            name=name,
            kind=kind,
            remove_python_package_func=adapters.remove_python_package,
            remove_vsrepo_package_func=adapters.remove_vsrepo_package,
            sync_environment_func=adapters.sync_environment,
            environment_errors=adapters.environment_errors,
        )
    except VapourSynthEnvironmentWorkflowError as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    typer.echo(f"Package removed: {name}")
    typer.echo(f"Environment: {environment.environment_id}")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _echo_scan_result(result: InventoryScanResult) -> None:
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


def _echo_queue_counts(database_url: str) -> None:
    with db_session(database_url) as session:
        status = scheduler_status(scheduler_status_store(session), now=_utc_now())
        jobs_by_status = status.counts_by_status
    typer.echo("Queue:")
    typer.echo(f"  queued:              {jobs_by_status[JobStatus.QUEUED]}")
    typer.echo(f"  encoding:            {jobs_by_status[JobStatus.ENCODING]}")
    typer.echo(f"  encoded:             {jobs_by_status[JobStatus.ENCODED]}")
    typer.echo(f"  validating:          {jobs_by_status[JobStatus.VALIDATING]}")
    typer.echo(f"  ready to promote:    {jobs_by_status[JobStatus.READY_TO_PROMOTE]}")
    typer.echo(f"  promoting:           {jobs_by_status[JobStatus.PROMOTING]}")
    typer.echo(f"  cleaning:            {jobs_by_status[JobStatus.CLEANING]}")
    typer.echo(f"  promoted:            {jobs_by_status[JobStatus.PROMOTED]}")
    typer.echo(f"  size rejected:       {jobs_by_status[JobStatus.SIZE_REJECTED]}")
    typer.echo(f"  validation failed:   {jobs_by_status[JobStatus.VALIDATION_FAILED]}")
    typer.echo(f"  failed:              {jobs_by_status[JobStatus.FAILED]}")


def _echo_recent_failed_jobs(database_url: str, *, limit: int = 5) -> None:
    with db_session(database_url) as session:
        failed_jobs = recent_failed_jobs(job_view_store(session), limit=limit)

    if not failed_jobs:
        return

    typer.echo("")
    typer.echo("Failed jobs:")
    for job in failed_jobs:
        typer.echo(f"  {job.id:<3} {job_stage_value(job.stage):<9} {job.file_name}")
        echo_error_block(job.last_error_type, job.last_error_message, indent="    ")
        if job.latest_attempt is not None:
            echo_attempt_log_status(job.latest_attempt, indent="    ")


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


def _wait_for_scheduler_inactive(database_url: str, *, timeout_seconds: float) -> bool:
    def load_lease_state() -> str:
        with db_session(database_url) as session:
            status = scheduler_status(scheduler_status_store(session), now=_utc_now())
            return status.lease_state

    return wait_for_scheduler_inactive(
        load_lease_state=load_lease_state,
        timeout_seconds=timeout_seconds,
        monotonic=time.monotonic,
        sleep=time.sleep,
    )


def _wait_for_job_status(
    database_url: str,
    job_id: int,
    expected: JobStatus,
    timeout_seconds: float,
) -> bool:
    def load_job_status() -> JobStatus | None:
        with db_session(database_url) as session:
            job = job_details(job_view_store(session), job_id=job_id)
            return None if job is None else job.status

    return wait_for_job_status(
        load_job_status=load_job_status,
        expected=expected,
        timeout_seconds=timeout_seconds,
        monotonic=time.monotonic,
        sleep=time.sleep,
    )


def _wait_for_queue_clear(database_url: str, *, timeout_seconds: float) -> bool:
    def has_running_requests() -> bool:
        with db_session(database_url) as session:
            return has_running_cancel_requests(queue_control_store(session))

    return wait_for_queue_clear(
        has_running_cancel_requests=has_running_requests,
        timeout_seconds=timeout_seconds,
        monotonic=time.monotonic,
        sleep=time.sleep,
    )


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


app.add_typer(db_app, name="db")
app.add_typer(scheduler_app, name="scheduler")
app.add_typer(jobs_app, name="jobs")
app.add_typer(files_app, name="files")
app.add_typer(plans_app, name="plans")
app.add_typer(workspace_app, name="workspace")
app.add_typer(config_app, name="config")
app.add_typer(workflow_app, name="workflow")
app.add_typer(profiles_app, name="profiles")
vpy_app.add_typer(vpy_scaffold_app, name="scaffold")
vpy_app.add_typer(vpy_env_app, name="env")
vpy_app.add_typer(vpy_packages_app, name="packages")
app.add_typer(vpy_app, name="vpy")
