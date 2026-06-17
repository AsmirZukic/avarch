from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ProfileRow:
    name: str
    origin: str
    description: str
    source_path: Path | None
    effective_hash: str
    tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProfileSnapshot:
    profiles: tuple[ProfileRow, ...]


@dataclass(frozen=True, slots=True)
class ProfileDetailSnapshot:
    profile: ProfileRow
    definition_hash: str
    vapoursynth_mode: str
    explanation: str
    known_limitations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CopyProfileRequest:
    source_profile_name: str
    new_profile_name: str
    destination_path: Path


@dataclass(frozen=True, slots=True)
class CopyProfileResult:
    created_path: Path
    profile_name: str


@dataclass(frozen=True, slots=True)
class ScaffoldVpyRequest:
    source_profile_name: str
    template_output_path: Path
    companion_profile_path: Path | None = None
    companion_profile_name: str | None = None


@dataclass(frozen=True, slots=True)
class ScaffoldVpyResult:
    created_paths: tuple[Path, ...]
