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

RESERVED_BUILTIN_PROFILE_NAMES = frozenset({"default", "anime", "web_archive"})


class ProfileOrigin(StrEnum):
    BUILTIN = "builtin"
    USER = "user"


class ProfileRegistryError(RuntimeError):
    pass


class DuplicateProfileNameError(ProfileRegistryError):
    pass


class ReservedProfileNameError(ProfileRegistryError):
    pass


class NoProfilesFoundError(ProfileRegistryError):
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


@dataclass(frozen=True, slots=True)
class ProfileValidationIssue:
    source: str
    error: str


@dataclass(frozen=True, slots=True)
class ProfileValidationSummary:
    valid: tuple[ResolvedProfile, ...]
    invalid: tuple[ProfileValidationIssue, ...]

    @property
    def ok(self) -> bool:
        return not self.invalid


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
        for profile in user_profiles:
            if profile.name in RESERVED_BUILTIN_PROFILE_NAMES:
                raise ReservedProfileNameError(
                    f"User profile uses reserved built-in name: {profile.name} ({profile.source})"
                )
        visible_builtins = tuple(
            profile
            for profile in builtins
            if profile.name not in user_names or profile.name in RESERVED_BUILTIN_PROFILE_NAMES
        )
        return cls((*visible_builtins, *user_profiles))

    @classmethod
    def load(cls, *, search_paths: list[Path]) -> ProfileRegistry:
        profiles = _load_user_profile_search_paths(search_paths)
        if not profiles:
            locations = ", ".join(str(path) for path in search_paths) or "(none configured)"
            raise NoProfilesFoundError(
                "No profiles found in configured search paths.\n"
                f"Check: {locations}"
            )
        return cls(tuple(profiles))

    def list_profiles(self) -> tuple[ResolvedProfile, ...]:
        return self._profiles

    def get(self, name: str) -> ResolvedProfile:
        profile = self._by_name.get(name)
        if profile is None:
            raise UnknownProfileError(f"Unknown profile: {name}")
        return profile

    def validate_all(self) -> ProfileValidationSummary:
        return ProfileValidationSummary(valid=self._profiles, invalid=())


PROFILE_DIRECTORY_README = """Avarch profile documents live in this directory.

Inspect the .toml files here to see which profiles are available.
Edit a copied profile or add a new .toml file to create your own profile.
Update [profile_registry].search_paths in .avarch/config.toml if you want profiles in a
different location.
"""


def seed_packaged_profiles(search_path: Path, *, overwrite: bool = False) -> tuple[Path, ...]:
    search_path.mkdir(parents=True, exist_ok=True)
    seeded: list[Path] = []
    for resource in _packaged_profile_resources():
        if resource.name == "default.toml":
            continue
        destination = search_path / resource.name
        if destination.exists() and not overwrite:
            continue
        destination.write_text(resource.read_text(encoding="utf-8"), encoding="utf-8")
        seeded.append(destination)

    readme_path = search_path / "README.md"
    if overwrite or not readme_path.exists():
        readme_path.write_text(PROFILE_DIRECTORY_README, encoding="utf-8")

    return tuple(seeded)


def packaged_profile_names() -> tuple[str, ...]:
    return tuple(Path(resource.name).stem for resource in _packaged_profile_resources())


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
                base_dir=path.parent,
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
                base_dir=None,
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
    base_dir: Path | None,
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
