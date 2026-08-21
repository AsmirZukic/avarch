from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

from pydantic import ValidationError

from avarch.adapters.vapoursynth import validate_script_syntax
from avarch.application.planning import PlanningError
from avarch.application.vapoursynth_identity import (
    ResolvedVapourSynthFilter,
    ResolvedVapourSynthTemplate,
)
from avarch.models.plan import PlanArtifactPaths, TranscodePlan
from avarch.serialization import canonical_json


class PlanArtifactConflictError(PlanningError):
    pass


class PlanArtifactLoadError(PlanningError):
    pass


def load_plan_artifact(path: Path) -> TranscodePlan:
    try:
        return TranscodePlan.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        raise PlanArtifactLoadError(f"Unable to load plan artifact: {path}") from exc


def write_plan_artifacts(
    *,
    plan: TranscodePlan,
    vapoursynth_script: str,
    user_filter: ResolvedVapourSynthFilter | None = None,
    template: ResolvedVapourSynthTemplate | None = None,
) -> PlanArtifactPaths:
    if plan.vapoursynth.mode == "custom_template" and template is None:
        raise PlanningError("custom template plans must snapshot the resolved template")
    if plan.vapoursynth.mode != "custom_template" and template is not None:
        raise PlanningError("template snapshot was provided for a non-template plan")
    validate_script_syntax(vapoursynth_script)
    paths = plan.artifacts
    artifact_dir = paths.artifact_dir
    parent = artifact_dir.parent
    parent.mkdir(parents=True, exist_ok=True)

    artifact_payloads = _artifact_payloads(
        plan,
        vapoursynth_script=vapoursynth_script,
        user_filter=user_filter,
        template=template,
    )
    if artifact_dir.exists():
        if _artifact_dir_matches(artifact_payloads, artifact_dir):
            return paths
        raise PlanArtifactConflictError(f"Plan artifact bundle already exists: {artifact_dir}")

    temp_dir = Path(
        tempfile.mkdtemp(
            prefix=f".{artifact_dir.name}.tmp-",
            dir=parent,
        )
    )
    try:
        for relative_path, content in artifact_payloads.items():
            path = temp_dir / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8", newline="\n") as output_file:
                output_file.write(content)
                output_file.flush()
                os.fsync(output_file.fileno())
        os.replace(temp_dir, artifact_dir)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise

    return paths


def _artifact_payloads(
    plan: TranscodePlan,
    *,
    vapoursynth_script: str,
    user_filter: ResolvedVapourSynthFilter | None = None,
    template: ResolvedVapourSynthTemplate | None = None,
) -> dict[str, str]:
    payloads = {
        "plan.json": canonical_json(plan) + "\n",
        plan.artifacts.vapoursynth_script.name: vapoursynth_script,
        "av1an.command.json": canonical_json(plan.av1an) + "\n",
        "validation-policy.json": canonical_json(plan.validation) + "\n",
        "vpy/environment-lock.toml": _environment_lock_payload(plan),
        "vpy/snapshot.json": _vpy_snapshot_payload(plan),
    }
    if user_filter is not None:
        payloads["vpy/user_filter.py"] = user_filter.text
    if template is not None:
        payloads["vpy/custom_template.vpy"] = template.text
    return payloads


def _environment_lock_payload(plan: TranscodePlan) -> str:
    manifest_hash = plan.vapoursynth.environment_manifest_hash or "unknown"
    vapoursynth_version = plan.vapoursynth.vapoursynth_version or "unknown"
    lines = [
        "schema_version = 1",
        f'environment_id = "{_toml_escape(plan.vapoursynth.environment_id or "unknown")}"',
        f'avarch_image_digest = "{_toml_escape(plan.vapoursynth.avarch_image_digest)}"',
        f'manifest_hash = "{_toml_escape(manifest_hash)}"',
        f'python_version = "{_toml_escape(plan.vapoursynth.python_version or "unknown")}"',
        f'python_abi = "{_toml_escape(plan.vapoursynth.python_abi or "unknown")}"',
        f'platform = "{_toml_escape(plan.vapoursynth.runtime_platform or "unknown")}"',
        f'vapoursynth_version = "{_toml_escape(vapoursynth_version)}"',
        "",
    ]
    for namespace, digest in sorted(plan.vapoursynth.native_plugin_hashes.items()):
        lines.extend(
            (
                "[[plugins]]",
                f'namespace = "{_toml_escape(namespace)}"',
                f'sha256 = "{_toml_escape(digest)}"',
                "",
            )
        )
    return "\n".join(lines)


def _vpy_snapshot_payload(plan: TranscodePlan) -> str:
    payload = {
        "avarch_image_digest": plan.vapoursynth.avarch_image_digest,
        "environment_id": plan.vapoursynth.environment_id,
        "environment_manifest_hash": plan.vapoursynth.environment_manifest_hash,
        "filter_api_version": plan.vapoursynth.filter_api_version,
        "filter_entrypoint": plan.vapoursynth.filter_entrypoint,
        "filter_hash": plan.vapoursynth.filter_hash,
        "generator_version": plan.vapoursynth.generator_version,
        "mode": plan.vapoursynth.mode,
        "native_plugin_hashes": plan.vapoursynth.native_plugin_hashes,
        "plan_hash": plan.plan_hash,
        "profile_hash": plan.profile_hash,
        "template_api_version": plan.vapoursynth.template_api_version,
        "template_hash": plan.vapoursynth.template_hash,
        "vapoursynth_identity_hash": plan.vapoursynth.identity_hash,
        "vapoursynth_version": plan.vapoursynth.vapoursynth_version,
    }
    return canonical_json(payload) + "\n"


def _toml_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _artifact_dir_matches(payloads: dict[str, str], artifact_dir: Path) -> bool:
    if not artifact_dir.is_dir():
        return False
    existing = {
        str(path.relative_to(artifact_dir)) for path in artifact_dir.rglob("*") if path.is_file()
    }
    if existing != set(payloads):
        return False
    for relative_path, expected_content in payloads.items():
        path = artifact_dir / relative_path
        if not path.is_file():
            return False
        if path.read_text(encoding="utf-8") != expected_content:
            return False
    return True
