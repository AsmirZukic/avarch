from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from avarch.config import AppConfig
from avarch.profiles.models import ProfileDocument
from avarch.profiles.registry import (
    ProfileOrigin,
    ProfileRegistry,
    ProfileRegistryError,
    ResolvedProfile,
    UnknownProfileError,
)


class ProfileManagementError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ProfileListItem:
    name: str
    origin: str
    source: str


@dataclass(frozen=True, slots=True)
class ProfileCopyResult:
    destination: Path


def list_available_profiles(config: AppConfig) -> list[ProfileListItem]:
    try:
        profiles = ProfileRegistry.from_config(config).list_profiles()
    except ProfileRegistryError as exc:
        raise ProfileManagementError(str(exc)) from exc
    return [
        ProfileListItem(name=profile.name, origin=profile.origin.value, source=profile.source)
        for profile in profiles
    ]


def copy_builtin_profile(
    config: AppConfig,
    *,
    source_name: str,
    name: str,
) -> ProfileCopyResult:
    try:
        registry = ProfileRegistry.from_config(config)
        source = registry.get(source_name)
    except (ProfileRegistryError, UnknownProfileError) as exc:
        raise ProfileManagementError(str(exc)) from exc

    if source.origin != ProfileOrigin.BUILTIN:
        raise ProfileManagementError(f"Can only copy built-in profiles: {source_name}")
    if name in {profile.name for profile in registry.list_profiles()}:
        raise ProfileManagementError(f"Profile name is already in use or reserved: {name}")

    destination_dir = _primary_profile_search_path(config)
    if destination_dir is None:
        raise ProfileManagementError("No profile search path is configured.")
    destination_dir.mkdir(parents=True, exist_ok=True)
    destination = destination_dir / f"{name}.toml"
    if destination.exists():
        raise ProfileManagementError(f"Profile already exists: {destination}")

    document = source.document.model_copy(update={"name": name})
    destination.write_text(render_profile_document(document), encoding="utf-8")
    return ProfileCopyResult(destination=destination)


def profile_registry(config: AppConfig) -> ProfileRegistry:
    try:
        return ProfileRegistry.from_config(config)
    except ProfileRegistryError as exc:
        raise ProfileManagementError(str(exc)) from exc


def resolve_profile(config: AppConfig, name: str) -> ResolvedProfile:
    try:
        return profile_registry(config).get(name)
    except UnknownProfileError as exc:
        raise ProfileManagementError(str(exc)) from exc


def render_profile_document(document: ProfileDocument) -> str:
    lines: list[str] = [
        f"schema_version = {document.schema_version}",
        f"name = {_toml_escape(document.name)}",
    ]
    if document.description is not None:
        lines.append(f"description = {_toml_escape(document.description)}")
    lines.extend(
        [
            f"tags = {_toml_string_list(document.tags)}",
            f"known_limitations = {_toml_string_list(document.known_limitations)}",
            "",
        ]
    )
    lines.extend(
        [
        f"backend = {_toml_escape(document.backend)}",
        f"container = {_toml_escape(document.container)}",
        "",
        "[match]",
        f"video_codec_not = {_toml_string_list(document.match.video_codec_not)}",
        "",
        "[video]",
        f"max_width = {document.video.max_width}",
        f"hdr_to_sdr = {_toml_bool(document.video.hdr_to_sdr)}",
        f"source = {_toml_escape(document.video.source)}",
        "",
        "[av1an]",
        f"encoder = {_toml_escape(document.av1an.encoder)}",
        f"workers = {_toml_scalar(document.av1an.workers)}",
        f"video_args = {_toml_escape(document.av1an.video_args)}",
        "",
        "[audio]",
        f"codec = {_toml_escape(document.audio.codec)}",
        f"bitrate = {_toml_escape(document.audio.bitrate)}",
        f"channels = {document.audio.channels}",
        f"languages = {_toml_string_list(document.audio.languages)}",
        "",
        "[subtitles]",
        f"languages = {_toml_string_list(document.subtitles.languages)}",
        f"keep_forced = {_toml_bool(document.subtitles.keep_forced)}",
        "",
        "[validation]",
        f"duration_tolerance_seconds = {document.validation.duration_tolerance_seconds:g}",
        f"minimum_output_bytes = {document.validation.minimum_output_bytes}",
        f"minimum_output_source_ratio = {document.validation.minimum_output_source_ratio:g}",
        f"decode_sample = {_toml_bool(document.validation.decode_sample)}",
        f"decode_sample_seconds = {document.validation.decode_sample_seconds:g}",
        ]
    )
    if document.validation.minimum_size_reduction_percent is not None:
        lines.append(
            "minimum_size_reduction_percent = "
            f"{document.validation.minimum_size_reduction_percent:g}"
        )
    lines.append("")
    lines.extend(
        [
            "[vapoursynth]",
            f"mode = {_toml_escape(document.vapoursynth.mode)}",
        ]
    )
    if document.vapoursynth.template is not None:
        lines.append(f"template = {_toml_escape(str(document.vapoursynth.template))}")
    if document.vapoursynth.script is not None:
        lines.append(f"script = {_toml_escape(str(document.vapoursynth.script))}")
    lines.append(f"entrypoint = {_toml_escape(document.vapoursynth.entrypoint)}")
    lines.append("")
    return "\n".join(lines)


def _primary_profile_search_path(config: AppConfig) -> Path | None:
    if not config.profile_registry.search_paths:
        return None
    return config.profile_registry.search_paths[0]


def _toml_string_list(values: list[str]) -> str:
    return "[" + ", ".join(_toml_escape(value) for value in values) + "]"


def _toml_escape(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"


def _toml_scalar(value: int | str) -> str:
    if isinstance(value, int):
        return str(value)
    return _toml_escape(value)
