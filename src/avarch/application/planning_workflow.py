from __future__ import annotations

from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from avarch.application.plan_artifacts import (
    MaterializedPlanArtifacts,
    PlanArtifactWriter,
    VapourSynthRuntimeChecker,
    VapourSynthScriptGenerator,
    build_transcode_plan_from_store,
    materialize_plan_artifacts,
)
from avarch.application.planning import (
    PlanningError,
    PlanningInputFile,
    PlanningProfile,
    PlanningRuntimeIdentity,
    PlanningStore,
    ProfileNotApplicableError,
    eligible_planning_inputs,
    equivalent_plan_exists,
    save_plan,
    select_planning_inputs,
)
from avarch.models.plan import TranscodePlan

ExceptionTypes = tuple[type[Exception], ...]


class PlanningWorkflowItemStatus(StrEnum):
    PLANNED = "planned"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class PlanningWorkflowItem:
    media_file: PlanningInputFile
    status: PlanningWorkflowItemStatus
    reason: str | None = None
    error_type: str | None = None
    runtime_validation_failed: bool = False
    plan: TranscodePlan | None = None
    runtime_checked: bool = False


@dataclass(frozen=True, slots=True)
class PlanningWorkflowResult:
    selected: int
    processed: int
    skipped: int
    failed: int
    items: tuple[PlanningWorkflowItem, ...]


def create_transcode_plan_for_file(
    *,
    open_store: Callable[[], AbstractContextManager[PlanningStore]],
    resolved_profile: PlanningProfile,
    input_path: Path,
    data_dir: Path,
    runtime_identity: PlanningRuntimeIdentity,
    generate_script: VapourSynthScriptGenerator,
    validate_script: Callable[[str], None],
    write_artifacts: PlanArtifactWriter,
    check_runtime: VapourSynthRuntimeChecker,
    runtime_env: dict[str, str],
    check_vpy: bool,
) -> MaterializedPlanArtifacts:
    with open_store() as store:
        artifacts = build_transcode_plan_from_store(
            store,
            input_path=input_path,
            resolved_profile=resolved_profile,
            data_dir=data_dir,
            runtime_identity=runtime_identity,
        )
    return materialize_plan_artifacts(
        artifacts,
        generate_script=generate_script,
        validate_script=validate_script,
        write_artifacts=write_artifacts,
        check_runtime=check_runtime,
        runtime_env=runtime_env,
        check_vpy=check_vpy,
    )


def create_transcode_plans(
    *,
    open_store: Callable[[], AbstractContextManager[PlanningStore]],
    resolved_profile: PlanningProfile,
    file_selectors: Sequence[Path] | None,
    workspace_root: Path,
    resolve_path: Callable[[Path], Path],
    data_dir: Path,
    runtime_identity: PlanningRuntimeIdentity,
    force: bool,
    now: datetime,
    generate_script: VapourSynthScriptGenerator,
    validate_script: Callable[[str], None],
    write_artifacts: PlanArtifactWriter,
    check_runtime: VapourSynthRuntimeChecker,
    runtime_env: dict[str, str],
    check_vpy: bool,
    failure_errors: ExceptionTypes = (),
    runtime_validation_errors: ExceptionTypes = (),
) -> PlanningWorkflowResult:
    with open_store() as store:
        if file_selectors:
            selection = select_planning_inputs(
                store,
                file_selectors=file_selectors,
                workspace_root=workspace_root,
                resolve_path=resolve_path,
            )
            if selection.missing:
                raise PlanningError(
                    f"File is not present in the media inventory: {selection.missing[0]}\n"
                    "Run avarch scan for the containing root first."
                )
            media_files = list(selection.selected)
        else:
            media_files = eligible_planning_inputs(store)

    items: list[PlanningWorkflowItem] = []
    for media_file in media_files:
        item = _plan_one_file(
            open_store=open_store,
            media_file=media_file,
            resolved_profile=resolved_profile,
            workspace_root=workspace_root,
            data_dir=data_dir,
            runtime_identity=runtime_identity,
            force=force,
            now=now,
            generate_script=generate_script,
            validate_script=validate_script,
            write_artifacts=write_artifacts,
            check_runtime=check_runtime,
            runtime_env=runtime_env,
            check_vpy=check_vpy,
            failure_errors=failure_errors,
            runtime_validation_errors=runtime_validation_errors,
        )
        items.append(item)

    return PlanningWorkflowResult(
        selected=len(media_files),
        processed=sum(1 for item in items if item.status == PlanningWorkflowItemStatus.PLANNED),
        skipped=sum(1 for item in items if item.status == PlanningWorkflowItemStatus.SKIPPED),
        failed=sum(1 for item in items if item.status == PlanningWorkflowItemStatus.FAILED),
        items=tuple(items),
    )


def _plan_one_file(
    *,
    open_store: Callable[[], AbstractContextManager[PlanningStore]],
    media_file: PlanningInputFile,
    resolved_profile: PlanningProfile,
    workspace_root: Path,
    data_dir: Path,
    runtime_identity: PlanningRuntimeIdentity,
    force: bool,
    now: datetime,
    generate_script: VapourSynthScriptGenerator,
    validate_script: Callable[[str], None],
    write_artifacts: PlanArtifactWriter,
    check_runtime: VapourSynthRuntimeChecker,
    runtime_env: dict[str, str],
    check_vpy: bool,
    failure_errors: ExceptionTypes,
    runtime_validation_errors: ExceptionTypes,
) -> PlanningWorkflowItem:
    if media_file.id is None:
        return PlanningWorkflowItem(
            media_file=media_file,
            status=PlanningWorkflowItemStatus.FAILED,
            reason="Media file must be persisted before planning.",
        )
    if media_file.probe_state == "missing":
        return PlanningWorkflowItem(
            media_file=media_file,
            status=PlanningWorkflowItemStatus.SKIPPED,
            reason="probe-missing",
        )
    if media_file.probe_state == "stale":
        return PlanningWorkflowItem(
            media_file=media_file,
            status=PlanningWorkflowItemStatus.SKIPPED,
            reason="probe-stale",
        )

    try:
        materialized = _build_and_materialize_plan(
            open_store=open_store,
            media_file=media_file,
            resolved_profile=resolved_profile,
            workspace_root=workspace_root,
            data_dir=data_dir,
            runtime_identity=runtime_identity,
            force=force,
            generate_script=generate_script,
            validate_script=validate_script,
            write_artifacts=write_artifacts,
            check_runtime=check_runtime,
            runtime_env=runtime_env,
            check_vpy=check_vpy,
        )
        if materialized is None:
            return PlanningWorkflowItem(
                media_file=media_file,
                status=PlanningWorkflowItemStatus.SKIPPED,
                reason="plan-current",
            )
        with open_store() as store:
            save_plan(store, plan=materialized.plan, now=now)
    except ProfileNotApplicableError as exc:
        return PlanningWorkflowItem(
            media_file=media_file,
            status=PlanningWorkflowItemStatus.SKIPPED,
            reason=f"profile-not-applicable: {', '.join(exc.reasons)}",
        )
    except failure_errors as exc:
        return PlanningWorkflowItem(
            media_file=media_file,
            status=PlanningWorkflowItemStatus.FAILED,
            reason=str(exc),
            error_type=exc.__class__.__name__,
            runtime_validation_failed=isinstance(exc, runtime_validation_errors),
        )

    return PlanningWorkflowItem(
        media_file=media_file,
        status=PlanningWorkflowItemStatus.PLANNED,
        plan=materialized.plan,
        runtime_checked=materialized.runtime_checked,
    )


def _build_and_materialize_plan(
    *,
    open_store: Callable[[], AbstractContextManager[PlanningStore]],
    media_file: PlanningInputFile,
    resolved_profile: PlanningProfile,
    workspace_root: Path,
    data_dir: Path,
    runtime_identity: PlanningRuntimeIdentity,
    force: bool,
    generate_script: VapourSynthScriptGenerator,
    validate_script: Callable[[str], None],
    write_artifacts: PlanArtifactWriter,
    check_runtime: VapourSynthRuntimeChecker,
    runtime_env: dict[str, str],
    check_vpy: bool,
) -> MaterializedPlanArtifacts | None:
    file_path = Path(media_file.path)
    if not file_path.is_absolute():
        file_path = workspace_root / file_path
    with open_store() as store:
        artifacts = build_transcode_plan_from_store(
            store,
            input_path=file_path,
            resolved_profile=resolved_profile,
            data_dir=data_dir,
            runtime_identity=runtime_identity,
        )
        plan = artifacts.plan
        if not force and equivalent_plan_exists(
            store,
            media_file_id=media_file.id,
            probe_hash=plan.probe_hash,
            profile_hash=plan.profile_hash,
            execution_identity_hash=plan.execution_identity.identity_hash,
        ):
            return None
    return materialize_plan_artifacts(
        artifacts,
        generate_script=generate_script,
        validate_script=validate_script,
        write_artifacts=write_artifacts,
        check_runtime=check_runtime,
        runtime_env=runtime_env,
        check_vpy=check_vpy,
    )
