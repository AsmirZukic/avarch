from pathlib import Path

import pytest

from avarch.adapters.filesystem.plans import (
    PlanArtifactConflictError,
    PlanArtifactLoadError,
    load_plan_artifact,
    write_plan_artifacts,
)
from avarch.adapters.vapoursynth import generate_builtin_script
from avarch.application.vapoursynth_identity import (
    ResolvedVapourSynthFilter,
    ResolvedVapourSynthTemplate,
)
from avarch.models.plan import TranscodePlan
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
    assert (artifact_dir / "vpy" / "environment-lock.toml").is_file()
    assert (artifact_dir / "vpy" / "snapshot.json").is_file()
    assert (artifact_dir / "movie.vpy").read_text(encoding="utf-8") == script


def test_identical_bundle_write_is_idempotent(tmp_path: Path) -> None:
    plan = _plan_for_dir(tmp_path / "bundle")
    script = generate_builtin_script(plan)

    first = write_plan_artifacts(plan=plan, vapoursynth_script=script)
    second = write_plan_artifacts(plan=plan, vapoursynth_script=script)

    assert first == second


def test_plan_artifact_loader_reads_plan_json(tmp_path: Path) -> None:
    plan = _plan_for_dir(tmp_path / "bundle")
    script = generate_builtin_script(plan)
    write_plan_artifacts(plan=plan, vapoursynth_script=script)

    loaded = load_plan_artifact(plan.artifacts.plan_json)

    assert loaded.plan_hash == plan.plan_hash


def test_plan_artifact_loader_rejects_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "plan.json"
    path.write_text("not json", encoding="utf-8")

    with pytest.raises(PlanArtifactLoadError):
        load_plan_artifact(path)


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


def test_custom_filter_bundle_snapshots_user_filter(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "bundle"
    plan = _plan_for_dir(artifact_dir).model_copy(
        update={
            "vapoursynth": _plan_for_dir(artifact_dir).vapoursynth.model_copy(
                update={
                    "mode": "custom_filter",
                    "filter_path": artifact_dir / "vpy" / "user_filter.py",
                    "filter_hash": "hash",
                    "filter_entrypoint": "apply",
                    "filter_api_version": 1,
                }
            )
        }
    )
    user_filter = ResolvedVapourSynthFilter(
        path=tmp_path / "my_filter.py",
        text="def apply(video, context):\n    return video\n",
        script_hash="hash",
        entrypoint="apply",
        api_version=1,
    )

    write_plan_artifacts(
        plan=plan,
        vapoursynth_script="# wrapper\n",
        user_filter=user_filter,
    )

    assert (artifact_dir / "vpy" / "user_filter.py").read_text(encoding="utf-8") == (
        "def apply(video, context):\n    return video\n"
    )


def test_custom_template_bundle_snapshots_user_template(tmp_path: Path) -> None:
    artifact_dir = tmp_path / "bundle"
    base = _plan_for_dir(artifact_dir)
    template = ResolvedVapourSynthTemplate(
        path=tmp_path / "my_pipeline.vpy",
        text="clip.set_output(index=0)\n",
        template_hash="template-hash",
    )
    plan = base.model_copy(
        update={
            "vapoursynth": base.vapoursynth.model_copy(
                update={
                    "mode": "custom_template",
                    "template_path": template.path,
                    "template_hash": template.template_hash,
                }
            )
        }
    )

    write_plan_artifacts(
        plan=plan,
        vapoursynth_script="clip.set_output(index=0)\n",
        template=template,
    )

    assert (artifact_dir / "vpy" / "custom_template.vpy").read_text(encoding="utf-8") == (
        "clip.set_output(index=0)\n"
    )


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
