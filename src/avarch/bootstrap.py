from __future__ import annotations

from collections.abc import Callable, Generator, Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session, col, select

from avarch.adapters.filesystem.plans import (
    PlanArtifactConflictError as PlanArtifactConflictError,
)
from avarch.adapters.filesystem.plans import (
    write_plan_artifacts as write_plan_artifacts,
)
from avarch.adapters.filesystem.workspace import (
    WorkspaceContext as WorkspaceContext,
)
from avarch.adapters.filesystem.workspace import (
    WorkspaceError as WorkspaceError,
)
from avarch.adapters.filesystem.workspace import (
    create_workspace as create_workspace,
)
from avarch.adapters.inventory_scan import SqliteInventoryScanWorkflow
from avarch.adapters.manual_validation import SchedulerManualValidationWorker
from avarch.adapters.probe import (
    FfprobeCollector as FfprobeCollector,
)
from avarch.adapters.probe import (
    run_ffprobe as run_ffprobe,
)
from avarch.adapters.promotion import PromotionWorkflowAdapter
from avarch.adapters.scheduler_process import SchedulerProcessAdapter
from avarch.adapters.scheduler_run import SchedulerRuntimeAdapter, SchedulerWorkerAdapter
from avarch.adapters.sqlite.admin import SqliteDatabaseAdmin
from avarch.adapters.sqlite.db import UnsupportedDatabaseSchemaError, create_db_engine
from avarch.adapters.sqlite.enqueue import SqliteEnqueueStore
from avarch.adapters.sqlite.file_views import SqliteFileViewStore
from avarch.adapters.sqlite.job_control_store import SqliteJobControlStore
from avarch.adapters.sqlite.job_views import SqliteJobViewStore
from avarch.adapters.sqlite.manual_validation import SqliteManualValidationPreparationStore
from avarch.adapters.sqlite.migrations import upgrade_database
from avarch.adapters.sqlite.models import JobAttempt
from avarch.adapters.sqlite.plan_views import SqlitePlanViewStore
from avarch.adapters.sqlite.planning import SqlitePlanningUnitOfWork
from avarch.adapters.sqlite.probing import SqliteProbeStore
from avarch.adapters.sqlite.queue_control import SqliteQueueControlStore, SqliteQueueRetryStore
from avarch.adapters.sqlite.scheduler_control import SqliteSchedulerControlStore
from avarch.adapters.sqlite.scheduler_snapshot import SqliteSchedulerSnapshotQuery
from avarch.adapters.sqlite.scheduler_status import SqliteSchedulerStatusStore
from avarch.adapters.sqlite.urls import resolve_database_url
from avarch.adapters.system.resource_telemetry import LinuxResourceSampler, OutputGrowthSampler
from avarch.adapters.vapoursynth import (
    VapourSynthGenerationError as VapourSynthGenerationError,
)
from avarch.adapters.vapoursynth import (
    VspipeError as VspipeError,
)
from avarch.adapters.vapoursynth import (
    check_vapoursynth_script as check_vapoursynth_script,
)
from avarch.adapters.vapoursynth import (
    generate_vapoursynth_script as generate_vapoursynth_script,
)
from avarch.adapters.vapoursynth import (
    validate_script_syntax as validate_script_syntax,
)
from avarch.adapters.vpy_env import (
    VpyEnvironmentError as VpyEnvironmentError,
)
from avarch.adapters.vpy_env import (
    VpyRequirements as VpyRequirements,
)
from avarch.adapters.vpy_env import (
    VsrepoUnavailableError as VsrepoUnavailableError,
)
from avarch.adapters.vpy_env import (
    add_python_package as add_python_package,
)
from avarch.adapters.vpy_env import (
    add_vsrepo_package as add_vsrepo_package,
)
from avarch.adapters.vpy_env import (
    build_runtime_identity as build_runtime_identity,
)
from avarch.adapters.vpy_env import (
    load_requirements as load_requirements,
)
from avarch.adapters.vpy_env import (
    planning_runtime_identity_for_data_dir as planning_runtime_identity_for_data_dir,
)
from avarch.adapters.vpy_env import (
    remove_python_package as remove_python_package,
)
from avarch.adapters.vpy_env import (
    remove_vsrepo_package as remove_vsrepo_package,
)
from avarch.adapters.vpy_env import (
    runtime_environment_variables as runtime_environment_variables,
)
from avarch.adapters.vpy_env import (
    search_vsrepo_packages as search_vsrepo_packages,
)
from avarch.adapters.vpy_env import (
    sync_environment as sync_environment,
)
from avarch.adapters.vpy_plugins import (
    VpyPluginInventoryError as VpyPluginInventoryError,
)
from avarch.adapters.vpy_plugins import (
    list_vapoursynth_plugins as list_vapoursynth_plugins,
)
from avarch.application.plan_artifacts import (
    PlanArtifactWriter,
    VapourSynthRuntimeChecker,
    VapourSynthScriptGenerator,
)
from avarch.application.planning import PlanningRuntimeIdentity
from avarch.application.resource_telemetry import (
    CompositeResourceSampler,
    NullResourceSampler,
    ResourceSampler,
)
from avarch.config import AppConfig
from avarch.domain.jobs import AttemptStatus, JobStage

__all__ = [
    "DatabaseSchemaUpgradeError",
    "PlanArtifactConflictError",
    "VapourSynthGenerationError",
    "VapourSynthEnvironmentAdapters",
    "VpyRequirements",
    "VspipeError",
    "VapourSynthPlanningRuntime",
    "WorkspaceContext",
    "WorkspaceError",
    "create_workspace",
    "database_admin",
    "database_admin_errors",
    "db_session",
    "db_transaction",
    "enqueue_store",
    "file_view_store",
    "ffprobe_collector",
    "inventory_scan_workflow",
    "job_control_store",
    "job_view_store",
    "manual_validation_preparation_store",
    "manual_validation_worker",
    "plan_view_store",
    "planning_unit_of_work",
    "probe_store",
    "promotion_workflow",
    "queue_control_store",
    "queue_retry_store",
    "scheduler_control_store",
    "scheduler_process_controller",
    "scheduler_resource_sampler",
    "scheduler_snapshot_query",
    "scheduler_runner",
    "scheduler_status_store",
    "upgrade_database_schema",
    "vapoursynth_environment_adapters",
    "vapoursynth_planning_runtime",
    "workspace_database_url",
]


class DatabaseSchemaUpgradeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class VapourSynthPlanningRuntime:
    runtime_identity: PlanningRuntimeIdentity
    generate_script: VapourSynthScriptGenerator
    validate_script: Callable[[str], None]
    write_artifacts: PlanArtifactWriter
    check_runtime: VapourSynthRuntimeChecker
    runtime_env: dict[str, str]


@dataclass(frozen=True, slots=True)
class VapourSynthEnvironmentAdapters:
    sync_environment: Callable[[WorkspaceContext], Any]
    load_requirements: Callable[[WorkspaceContext], Any]
    build_runtime_identity: Callable[[Any], Any]
    list_plugins: Callable[[], Any]
    search_packages: Callable[[str], Any]
    add_python_package: Callable[[WorkspaceContext, str], Any]
    add_vsrepo_package: Callable[[WorkspaceContext, str], Any]
    remove_python_package: Callable[[WorkspaceContext, str], Any]
    remove_vsrepo_package: Callable[[WorkspaceContext, str], Any]
    environment_errors: tuple[type[Exception], ...]
    plugin_inventory_errors: tuple[type[Exception], ...]
    package_search_unavailable_errors: tuple[type[Exception], ...]


def workspace_database_url(config: AppConfig, config_path: Path) -> str:
    return resolve_database_url(config, config_path)


def upgrade_database_schema(database_url: str) -> None:
    try:
        upgrade_database(database_url)
    except UnsupportedDatabaseSchemaError as exc:
        raise DatabaseSchemaUpgradeError(str(exc)) from exc


def database_admin_errors() -> tuple[type[Exception], ...]:
    return (UnsupportedDatabaseSchemaError,)


@contextmanager
def db_session(database_url: str) -> Generator[Session]:
    engine = create_db_engine(database_url)
    with Session(engine) as session:
        yield session


@contextmanager
def db_transaction(database_url: str) -> Generator[Session]:
    engine = create_db_engine(database_url)
    with Session(engine) as session, session.begin():
        yield session


def database_admin(database_url: str) -> SqliteDatabaseAdmin:
    return SqliteDatabaseAdmin(database_url)


def enqueue_store(session: Session) -> SqliteEnqueueStore:
    return SqliteEnqueueStore(session)


def file_view_store(session: Session) -> SqliteFileViewStore:
    return SqliteFileViewStore(session)


def job_control_store(session: Session) -> SqliteJobControlStore:
    return SqliteJobControlStore(session)


def job_view_store(session: Session) -> SqliteJobViewStore:
    return SqliteJobViewStore(session)


def plan_view_store(session: Session) -> SqlitePlanViewStore:
    return SqlitePlanViewStore(session)


def planning_unit_of_work(database_url: str) -> Callable[[], SqlitePlanningUnitOfWork]:
    engine = create_db_engine(database_url)
    return lambda: SqlitePlanningUnitOfWork(engine)


def probe_store(session: Session) -> SqliteProbeStore:
    return SqliteProbeStore(session)


def ffprobe_collector(
    *,
    probe_runner: Callable[[Path], dict[str, Any]] | None = None,
) -> FfprobeCollector:
    return FfprobeCollector(probe_runner=run_ffprobe if probe_runner is None else probe_runner)


def vapoursynth_planning_runtime(
    workspace: WorkspaceContext,
    *,
    data_dir: Path,
    generate_script: VapourSynthScriptGenerator | None = None,
    validate_script: Callable[[str], None] | None = None,
    write_artifacts: PlanArtifactWriter | None = None,
    check_runtime: VapourSynthRuntimeChecker | None = None,
) -> VapourSynthPlanningRuntime:
    return VapourSynthPlanningRuntime(
        runtime_identity=planning_runtime_identity_for_data_dir(data_dir),
        generate_script=generate_vapoursynth_script if generate_script is None else generate_script,
        validate_script=validate_script_syntax if validate_script is None else validate_script,
        write_artifacts=write_plan_artifacts if write_artifacts is None else write_artifacts,
        check_runtime=check_vapoursynth_script if check_runtime is None else check_runtime,
        runtime_env=runtime_environment_variables(workspace),
    )


def vapoursynth_environment_adapters(
    *,
    sync_environment_func: Callable[[WorkspaceContext], Any] | None = None,
    load_requirements_func: Callable[[WorkspaceContext], Any] | None = None,
    build_runtime_identity_func: Callable[[Any], Any] | None = None,
    list_plugins_func: Callable[[], Any] | None = None,
    search_packages_func: Callable[[str], Any] | None = None,
    add_python_package_func: Callable[[WorkspaceContext, str], Any] | None = None,
    add_vsrepo_package_func: Callable[[WorkspaceContext, str], Any] | None = None,
    remove_python_package_func: Callable[[WorkspaceContext, str], Any] | None = None,
    remove_vsrepo_package_func: Callable[[WorkspaceContext, str], Any] | None = None,
) -> VapourSynthEnvironmentAdapters:
    sync_func = sync_environment if sync_environment_func is None else sync_environment_func
    load_func = load_requirements if load_requirements_func is None else load_requirements_func
    return VapourSynthEnvironmentAdapters(
        sync_environment=sync_func,
        load_requirements=load_func,
        build_runtime_identity=(
            build_runtime_identity
            if build_runtime_identity_func is None
            else build_runtime_identity_func
        ),
        list_plugins=list_vapoursynth_plugins if list_plugins_func is None else list_plugins_func,
        search_packages=(
            search_vsrepo_packages if search_packages_func is None else search_packages_func
        ),
        add_python_package=(
            add_python_package if add_python_package_func is None else add_python_package_func
        ),
        add_vsrepo_package=(
            add_vsrepo_package if add_vsrepo_package_func is None else add_vsrepo_package_func
        ),
        remove_python_package=(
            remove_python_package
            if remove_python_package_func is None
            else remove_python_package_func
        ),
        remove_vsrepo_package=(
            remove_vsrepo_package
            if remove_vsrepo_package_func is None
            else remove_vsrepo_package_func
        ),
        environment_errors=(VpyEnvironmentError,),
        plugin_inventory_errors=(VpyPluginInventoryError,),
        package_search_unavailable_errors=(VsrepoUnavailableError,),
    )


def inventory_scan_workflow(*, database_url: str) -> SqliteInventoryScanWorkflow:
    return SqliteInventoryScanWorkflow(database_url=database_url)


def queue_control_store(session: Session) -> SqliteQueueControlStore:
    return SqliteQueueControlStore(session)


def queue_retry_store(session: Session, *, config: AppConfig) -> SqliteQueueRetryStore:
    return SqliteQueueRetryStore(session, config=config)


def scheduler_control_store(session: Session) -> SqliteSchedulerControlStore:
    return SqliteSchedulerControlStore(session)


def scheduler_status_store(session: Session) -> SqliteSchedulerStatusStore:
    return SqliteSchedulerStatusStore(session)


def scheduler_snapshot_query(
    session: Session,
    *,
    workspace_root: Path,
    database_url: str | None = None,
    now: Callable[[], datetime] | None = None,
) -> SqliteSchedulerSnapshotQuery:
    return SqliteSchedulerSnapshotQuery(
        session,
        workspace_root=str(workspace_root),
        database_url=database_url,
        now=now,
    )


def scheduler_resource_sampler(
    *,
    database_url: str,
    clock: Callable[[], datetime],
) -> ResourceSampler:
    if not _linux_platform():
        return NullResourceSampler(reason="unsupported_platform", clock=clock)
    return CompositeResourceSampler(
        (
            LinuxResourceSampler(clock=clock),
            OutputGrowthSampler(
                paths=lambda: _active_attempt_output_paths(database_url=database_url),
                clock=clock,
            ),
        ),
        clock=clock,
    )


def manual_validation_worker() -> SchedulerManualValidationWorker:
    return SchedulerManualValidationWorker()


def manual_validation_preparation_store(session: Session) -> SqliteManualValidationPreparationStore:
    return SqliteManualValidationPreparationStore(session)


def promotion_workflow() -> PromotionWorkflowAdapter:
    return PromotionWorkflowAdapter()


def scheduler_process_controller() -> SchedulerProcessAdapter:
    return SchedulerProcessAdapter()


def scheduler_runner(
    *,
    claimable_stages: Iterable[JobStage] | None = None,
) -> SchedulerRuntimeAdapter:
    return SchedulerRuntimeAdapter(
        workers=SchedulerWorkerAdapter(promotion_workflow_factory=promotion_workflow),
        claimable_stages=claimable_stages,
    )


def _linux_platform() -> bool:
    import sys

    return sys.platform.startswith("linux")


def _active_attempt_output_paths(*, database_url: str) -> tuple[Path, ...]:
    with db_session(database_url) as session:
        attempts = tuple(
            session.exec(
                select(JobAttempt).where(col(JobAttempt.status) == AttemptStatus.RUNNING)
            ).all()
        )
    paths: list[Path] = []
    for attempt in attempts:
        if attempt.output_path is not None:
            paths.append(Path(attempt.output_path))
        if attempt.temp_dir is not None:
            paths.extend(_regular_files_under(Path(attempt.temp_dir)))
    return tuple(dict.fromkeys(paths))


def _regular_files_under(path: Path) -> tuple[Path, ...]:
    try:
        return tuple(child for child in path.rglob("*") if child.is_file())
    except OSError:
        return ()
