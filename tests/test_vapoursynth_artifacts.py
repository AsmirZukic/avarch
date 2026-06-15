from pathlib import Path

import pytest

from avarch.models.plan import TranscodePlan
from avarch.planner import PlanArtifactConflictError, write_plan_artifacts
from avarch.vapoursynth import generate_builtin_script
from tests.test_plan_models import sample_plan


def test_bundle_contains_all_four_artifacts(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "bundle"
    plan = _plan_for_dir(artifact_dir)
    script = generate_builtin_script(plan)

    write_plan_artifacts(plan=plan, vapoursynth_script=script)

    assert (artifact_dir / "plan.json").is_file()
    assert (artifact_dir / "movie.vpy").is_file()
    assert (artifact_dir / "av1an.command.json").is_file()
    assert (artifact_dir / "validation-policy.json").is_file()
    assert (artifact_dir / "movie.vpy").read_text(encoding="utf-8") == script


def test_identical_bundle_write_is_idempotent(tmp_path: Path) -> None:
    plan = _plan_for_dir(tmp_path / "bundle")
    script = generate_builtin_script(plan)

    first = write_plan_artifacts(plan=plan, vapoursynth_script=script)
    second = write_plan_artifacts(plan=plan, vapoursynth_script=script)

    assert first == second


def test_missing_existing_artifact_causes_conflict(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "bundle"
    plan = _plan_for_dir(artifact_dir)
    script = generate_builtin_script(plan)
    write_plan_artifacts(plan=plan, vapoursynth_script=script)
    (artifact_dir / "movie.vpy").unlink()

    with pytest.raises(PlanArtifactConflictError):
        write_plan_artifacts(plan=plan, vapoursynth_script=script)


def test_unexpected_existing_file_causes_conflict(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "bundle"
    plan = _plan_for_dir(artifact_dir)
    script = generate_builtin_script(plan)
    write_plan_artifacts(plan=plan, vapoursynth_script=script)
    (artifact_dir / "extra.txt").write_text("extra", encoding="utf-8")

    with pytest.raises(PlanArtifactConflictError):
        write_plan_artifacts(plan=plan, vapoursynth_script=script)


def _plan_for_dir(artifact_dir: Path) -> TranscodePlan:
    base = sample_plan()
    script_path = artifact_dir / "movie.vpy"
    return base.model_copy(
        update={
            "artifacts": base.artifacts.model_copy(
                update={
                    "artifact_dir": artifact_dir,
                    "plan_json": artifact_dir / "plan.json",
                    "vapoursynth_script": script_path,
                    "av1an_command_json": artifact_dir / "av1an.command.json",
                    "validation_policy_json": artifact_dir / "validation-policy.json",
                }
            ),
            "vapoursynth": base.vapoursynth.model_copy(update={"script_path": script_path}),
            "av1an": base.av1an.model_copy(update={"input_path": script_path}),
        }
    )
