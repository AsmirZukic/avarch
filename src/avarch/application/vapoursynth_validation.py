from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path
from typing import Any

from avarch.application.vapoursynth_identity import (
    VapourSynthScriptPathError,
    resolve_vapoursynth_filter,
    resolve_vapoursynth_template,
    resolve_workspace_script_path,
)
from avarch.profiles.models import EncodingProfile


def validate_vapoursynth_profile_scripts(
    profile: EncodingProfile,
    *,
    workspace: Any,
    validate_script: Callable[[str], None],
) -> None:
    if profile.vapoursynth.mode == "generated":
        resolved_template = resolve_vapoursynth_template(profile)
        if resolved_template is not None:
            validate_script(resolved_template.text)
        return
    if profile.vapoursynth.mode == "custom_filter":
        if profile.vapoursynth.script is None:
            raise VapourSynthScriptPathError("custom_filter profile is missing script")
        script_path = resolve_workspace_script_path(
            workspace.scripts_dir,
            profile.vapoursynth.script,
        )
        script_text = _read_required_script(script_path)
        validate_script(script_text)
        _validate_filter_entrypoint(script_text, profile.vapoursynth.entrypoint)
        resolve_vapoursynth_filter(profile)
        return

    if profile.vapoursynth.template is None:
        raise VapourSynthScriptPathError("custom_template profile is missing template")
    template_path = resolve_workspace_script_path(
        workspace.scripts_dir,
        profile.vapoursynth.template,
    )
    template_text = _read_required_script(template_path)
    validate_script(template_text)


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
    raise VapourSynthScriptPathError(f"VapourSynth filter entrypoint was not found: {entrypoint}")
