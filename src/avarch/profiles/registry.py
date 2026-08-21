from __future__ import annotations

import tomllib
from dataclasses import dataclass
from enum import StrEnum
from importlib import resources
from importlib.resources.abc import Traversable
from pathlib import Path

from pydantic import ValidationError

from avarch.config import AppConfig
from avarch.profiles.models import EncodingProfile, ProfileDocument


class ProfileOrigin(StrEnum):
    BUILTIN = "builtin"
    USER = "user"


class ProfileRegistryError(RuntimeError):
    pass


class DuplicateProfileNameError(ProfileRegistryError):
    pass


class UnknownProfileError(ProfileRegistryError):
    pass


@dataclass(frozen=True, slots=True)
class ResolvedProfile:
    name: str
    document: ProfileDocument
    profile: EncodingProfile
    origin: ProfileOrigin
    source: str


class ProfileRegistry:
    def __init__(self, profiles: tuple[ResolvedProfile, ...]) -> None:
        by_name: dict[str, ResolvedProfile] = {}
        for profile in profiles:
            existing = by_name.get(profile.name)
            if existing is not None:
                raise DuplicateProfileNameError(
                    f"Duplicate profile name: {profile.name} ({existing.source}, {profile.source})"
                )
            by_name[profile.name] = profile
        self._profiles = tuple(sorted(profiles, key=lambda item: item.name))
        self._by_name = by_name

    @classmethod
    def from_config(cls, config: AppConfig) -> ProfileRegistry:
        builtins = _load_packaged_profiles()
        user_profiles = _load_user_profile_search_paths(config.profile_registry.search_paths)
        user_names = {profile.name for profile in user_profiles}
        visible_builtins = tuple(profile for profile in builtins if profile.name not in user_names)
        return cls((*visible_builtins, *user_profiles))

    def list_profiles(self) -> tuple[ResolvedProfile, ...]:
        return self._profiles

    def get(self, name: str) -> ResolvedProfile:
        profile = self._by_name.get(name)
        if profile is None:
            raise UnknownProfileError(f"Unknown profile: {name}")
        return profile


def _load_user_profile_search_paths(search_paths: list[Path]) -> list[ResolvedProfile]:
    profiles: list[ResolvedProfile] = []
    for search_path in search_paths:
        profiles.extend(_load_user_profiles(search_path))
    return profiles


def _load_user_profiles(search_path: Path) -> list[ResolvedProfile]:
    if not search_path.exists():
        return []
    if not search_path.is_dir():
        raise ProfileRegistryError(f"Profile search path is not a directory: {search_path}")
    profiles: list[ResolvedProfile] = []
    for path in sorted(search_path.glob("**/*.toml")):
        with path.open("rb") as profile_file:
            data = tomllib.load(profile_file)
        profiles.append(
            _resolved_profile(
                data,
                origin=ProfileOrigin.USER,
                source=str(path),
                scripts_dir=_scripts_dir_for_profile(path),
            )
        )
    return profiles


def _load_packaged_profiles() -> tuple[ResolvedProfile, ...]:
    profiles: list[ResolvedProfile] = []
    for resource in _packaged_profile_resources():
        data = tomllib.loads(resource.read_text(encoding="utf-8"))
        profiles.append(
            _resolved_profile(
                data,
                origin=ProfileOrigin.BUILTIN,
                source=f"packaged:{resource.name}",
                scripts_dir=None,
            )
        )
    return tuple(profiles)


def _packaged_profile_resources() -> tuple[Traversable, ...]:
    package = resources.files("avarch.profiles.builtin")
    return tuple(
        sorted(
            (resource for resource in package.iterdir() if resource.name.endswith(".toml")),
            key=lambda item: item.name,
        )
    )


def _resolved_profile(
    data: object,
    *,
    origin: ProfileOrigin,
    source: str,
    scripts_dir: Path | None,
) -> ResolvedProfile:
    try:
        document = ProfileDocument.model_validate(data)
    except ValidationError as exc:
        raise ProfileRegistryError(f"Invalid profile document: {source}") from exc
    if scripts_dir is not None:
        vapoursynth = document.vapoursynth
        updates: dict[str, Path] = {}
        if vapoursynth.script is not None and not vapoursynth.script.is_absolute():
            updates["script"] = scripts_dir / vapoursynth.script
        if vapoursynth.template is not None and not vapoursynth.template.is_absolute():
            updates["template"] = scripts_dir / vapoursynth.template
        if updates:
            document = document.model_copy(
                update={"vapoursynth": vapoursynth.model_copy(update=updates)}
            )
    return ResolvedProfile(
        name=document.name,
        document=document,
        profile=document.encoding_profile(),
        origin=origin,
        source=source,
    )


def _scripts_dir_for_profile(path: Path) -> Path | None:
    parts = path.parts
    try:
        avarch_index = len(parts) - 1 - parts[::-1].index(".avarch")
    except ValueError:
        return path.parent
    avarch_dir = Path(*parts[: avarch_index + 1])
    return avarch_dir / "scripts"
