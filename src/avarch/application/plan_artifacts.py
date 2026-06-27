from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from avarch.application.planning import (
    PlanningProfile,
    PlanningRuntimeIdentity,
    PlanningStore,
    build_plan,
    load_stored_planning_context,
)
from avarch.application.vapoursynth_identity import (
    ResolvedVapourSynthFilter,
    ResolvedVapourSynthTemplate,
    resolve_vapoursynth_filter,
    resolve_vapoursynth_template,
)
from avarch.models.plan import TranscodePlan


class VapourSynthScriptGenerator(Protocol):
    def __call__(
        self,
        plan: TranscodePlan,
        *,
        template: ResolvedVapourSynthTemplate | None,
        user_filter: ResolvedVapourSynthFilter | None,
    ) -> str: ...


class PlanArtifactWriter(Protocol):
    def __call__(
        self,
        *,
        plan: TranscodePlan,
        vapoursynth_script: str,
        user_filter: ResolvedVapourSynthFilter | None,
        template: ResolvedVapourSynthTemplate | None,
    ) -> object: ...


class VapourSynthRuntimeChecker(Protocol):
    def __call__(self, script_path: Path, *, env: dict[str, str] | None = None) -> object: ...


@dataclass(frozen=True, slots=True)
class BuiltPlanArtifacts:
    plan: TranscodePlan
    template: ResolvedVapourSynthTemplate | None
    user_filter: ResolvedVapourSynthFilter | None


@dataclass(frozen=True, slots=True)
class MaterializedPlanArtifacts:
    plan: TranscodePlan
    runtime_checked: bool


def build_transcode_plan_from_store(
    store: PlanningStore,
    *,
    input_path: Path,
    resolved_profile: PlanningProfile,
    data_dir: Path,
    runtime_identity: PlanningRuntimeIdentity,
) -> BuiltPlanArtifacts:
    context = load_stored_planning_context(
        store,
        input_path=input_path,
        resolved_profile=resolved_profile,
    )
    resolved_template = resolve_vapoursynth_template(context.profile)
    resolved_filter = resolve_vapoursynth_filter(context.profile)
    plan = build_plan(
        context,
        data_dir=data_dir,
        runtime_identity=runtime_identity,
        resolved_template=resolved_template,
        resolved_filter=resolved_filter,
    )
    return BuiltPlanArtifacts(
        plan=plan,
        template=resolved_template,
        user_filter=resolved_filter,
    )


def materialize_plan_artifacts(
    artifacts: BuiltPlanArtifacts,
    *,
    generate_script: VapourSynthScriptGenerator,
    validate_script: Callable[[str], None],
    write_artifacts: PlanArtifactWriter,
    check_runtime: VapourSynthRuntimeChecker,
    runtime_env: dict[str, str],
    check_vpy: bool,
) -> MaterializedPlanArtifacts:
    vapoursynth_script = generate_script(
        artifacts.plan,
        template=artifacts.template,
        user_filter=artifacts.user_filter,
    )
    validate_script(vapoursynth_script)
    write_artifacts(
        plan=artifacts.plan,
        vapoursynth_script=vapoursynth_script,
        user_filter=artifacts.user_filter,
        template=artifacts.template,
    )
    runtime_checked = False
    if check_vpy:
        check_runtime(
            artifacts.plan.vapoursynth.script_path,
            env=runtime_env,
        )
        runtime_checked = True
    return MaterializedPlanArtifacts(plan=artifacts.plan, runtime_checked=runtime_checked)
