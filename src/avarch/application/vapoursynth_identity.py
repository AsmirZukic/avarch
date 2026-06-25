from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from avarch.contracts import (
    VAPOURSYNTH_IDENTITY_HASH_CONTRACT,
    VAPOURSYNTH_SCRIPT_HASH_CONTRACT,
    VAPOURSYNTH_TEMPLATE_HASH_CONTRACT,
)
from avarch.models.plan import (
    VapourSynthMode,
    VapourSynthOutputFormat,
    VapourSynthResizeFilter,
)
from avarch.profiles.models import EncodingProfile
from avarch.serialization import canonical_json

GENERATOR_VERSION = 4


class VapourSynthGenerationError(RuntimeError):
    pass


class VapourSynthScriptPathError(VapourSynthGenerationError):
    pass


@dataclass(frozen=True, slots=True)
class ResolvedVapourSynthTemplate:
    path: Path
    text: str
    template_hash: str


@dataclass(frozen=True, slots=True)
class ResolvedVapourSynthFilter:
    path: Path
    text: str
    script_hash: str
    entrypoint: str
    api_version: int


def normalize_template_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def build_template_hash(text: str) -> str:
    normalized = normalize_template_text(text)
    payload = f"{VAPOURSYNTH_TEMPLATE_HASH_CONTRACT}\0".encode() + normalized.encode("utf-8")
    return hashlib.blake2b(payload, digest_size=32).hexdigest()


def build_script_hash(text: str) -> str:
    normalized = normalize_template_text(text)
    payload = f"{VAPOURSYNTH_SCRIPT_HASH_CONTRACT}\0".encode() + normalized.encode("utf-8")
    return hashlib.blake2b(payload, digest_size=32).hexdigest()


def resolve_vapoursynth_template(
    profile: EncodingProfile,
) -> ResolvedVapourSynthTemplate | None:
    template_path = (
        profile.vapoursynth.template
        if profile.vapoursynth.mode == "custom_template"
        else None
    )
    if template_path is None:
        return None

    text = normalize_template_text(template_path.read_bytes().decode("utf-8"))
    return ResolvedVapourSynthTemplate(
        path=template_path,
        text=text,
        template_hash=build_template_hash(text),
    )


def resolve_vapoursynth_filter(
    profile: EncodingProfile,
) -> ResolvedVapourSynthFilter | None:
    if profile.vapoursynth.mode != "custom_filter":
        return None
    script_path = profile.vapoursynth.script
    if script_path is None:
        raise VapourSynthScriptPathError("custom_filter profile is missing script")

    text = normalize_template_text(script_path.read_bytes().decode("utf-8"))
    return ResolvedVapourSynthFilter(
        path=script_path,
        text=text,
        script_hash=build_script_hash(text),
        entrypoint=profile.vapoursynth.entrypoint,
        api_version=profile.vapoursynth.api_version,
    )


def resolve_workspace_script_path(scripts_dir: Path, path: Path) -> Path:
    candidate = path if path.is_absolute() else scripts_dir / path
    scripts_dir = scripts_dir.resolve()
    resolved = candidate.resolve(strict=candidate.exists())
    try:
        resolved.relative_to(scripts_dir)
    except ValueError as exc:
        raise VapourSynthScriptPathError(
            f"VapourSynth script path escapes the workspace scripts directory: {path}"
        ) from exc
    return resolved


def build_vapoursynth_identity_hash(
    *,
    generator_version: int,
    mode: VapourSynthMode,
    output_format: VapourSynthOutputFormat,
    resize_filter: VapourSynthResizeFilter,
    template_hash: str | None,
    script_hash: str | None = None,
    filter_entrypoint: str | None = None,
    filter_api_version: int | None = None,
) -> str:
    if mode == "generated" and (
        template_hash is not None or script_hash is not None or filter_entrypoint is not None
    ):
        raise ValueError("generated mode must not include custom script data")
    if mode == "custom_template" and template_hash is None:
        raise ValueError("custom_template mode requires a template hash")
    if mode == "custom_template" and script_hash is not None:
        raise ValueError("custom_template mode must not include a script hash")
    if mode == "custom_filter" and script_hash is None:
        raise ValueError("custom_filter mode requires a script hash")
    if mode == "custom_filter" and template_hash is not None:
        raise ValueError("custom_filter mode must not include a template hash")

    payload_json = canonical_json(
        {
            "filter_api_version": filter_api_version,
            "filter_entrypoint": filter_entrypoint,
            "generator_version": generator_version,
            "mode": mode,
            "output_format": output_format,
            "resize_filter": resize_filter,
            "script_hash": script_hash,
            "template_hash": template_hash,
        }
    )
    payload = f"{VAPOURSYNTH_IDENTITY_HASH_CONTRACT}\0".encode() + payload_json.encode("utf-8")
    return hashlib.blake2b(payload, digest_size=32).hexdigest()
