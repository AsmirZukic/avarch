from __future__ import annotations

import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from avarch.application.resource_limits import ResourceLimitError, parse_memory_reserve

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
LogFormat = Literal["console", "json"]


class AppSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_dir: Path = Path("data")


class DatabaseSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = "sqlite:///data/avarch.adapters.sqlite.db"


class LoggingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: LogLevel = "INFO"
    format: LogFormat = "console"


class ResourceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cheap_workers: int = Field(default=4, ge=1)
    av1an_jobs: int = Field(default=1, ge=1)
    file_ops: int = Field(default=1, ge=1)
    cpu_reserve: float = Field(default=1.0, ge=0)
    memory_reserve: int | str = "10%"
    reservation_allocator: bool = True

    @field_validator("memory_reserve")
    @classmethod
    def validate_memory_reserve(cls, value: int | str) -> int | str:
        try:
            parse_memory_reserve(value)
        except ResourceLimitError as exc:
            raise ValueError(str(exc)) from exc
        return value

    @model_validator(mode="after")
    def block_unreserved_parallel_av1an(self) -> ResourceSettings:
        if self.av1an_jobs > 1 and not self.reservation_allocator:
            raise ValueError(
                "resources.av1an_jobs greater than 1 requires resource reservations, "
                "but resources.reservation_allocator is disabled"
            )
        return self


class PerformanceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    calibration_enabled: bool = True
    calibration_max_seconds: float = Field(default=120.0, ge=0)
    calibration_max_predicted_fraction: float = Field(default=0.01, ge=0)
    calibration_min_predicted_seconds: float = Field(default=300.0, ge=0)
    calibration_sample_seconds: float = Field(default=10.0, ge=5)
    calibration_warmup_seconds: float = Field(default=1.0, ge=0)
    calibration_max_candidates: int = Field(default=8, ge=2, le=12)
    calibration_min_gain_fraction: float = Field(default=0.03, ge=0, le=1)


class ProfileRegistrySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    search_paths: list[Path] = Field(default_factory=lambda: [Path("./profiles")])


def _default_roots() -> list[Path]:
    return []


def _default_extensions() -> set[str]:
    return {
        ".mkv",
        ".mp4",
        ".m4v",
        ".mov",
        ".avi",
        ".webm",
        ".ts",
        ".m2ts",
    }


def _default_exclude_directories() -> set[str]:
    return {
        ".avarch",
    }


class ScannerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    roots: list[Path] = Field(default_factory=_default_roots)
    extensions: set[str] = Field(default_factory=_default_extensions)
    exclude_directories: set[str] = Field(default_factory=_default_exclude_directories)

    @field_validator("extensions", mode="before")
    @classmethod
    def normalize_extensions(cls, value: object) -> set[str]:
        if value is None:
            return set()

        values = [value] if isinstance(value, str) else _iterable_values(value)

        normalized: set[str] = set()
        for extension in values:
            extension_text = str(extension).lower()
            if not extension_text.startswith("."):
                extension_text = f".{extension_text}"
            normalized.add(extension_text)

        return normalized


def _iterable_values(value: object) -> list[object]:
    if not isinstance(value, Iterable):
        msg = "extensions must be iterable"
        raise TypeError(msg)
    return list(cast(Iterable[object], value))


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app: AppSettings = AppSettings()
    database: DatabaseSettings = DatabaseSettings()
    logging: LoggingSettings = LoggingSettings()
    resources: ResourceSettings = ResourceSettings()
    performance: PerformanceSettings = PerformanceSettings()
    scanner: ScannerSettings = ScannerSettings()
    profile_registry: ProfileRegistrySettings = ProfileRegistrySettings()


WORKSPACE_CONFIG_TEXT = """[app]
data_dir = "data"

[database]
url = "sqlite:///data/avarch.adapters.sqlite.db"

[logging]
level = "INFO"
format = "console"

[resources]
cheap_workers = 4
av1an_jobs = 1
file_ops = 1
cpu_reserve = 1.0
memory_reserve = "10%"
reservation_allocator = true

[performance]
calibration_enabled = true
calibration_max_seconds = 120.0
calibration_max_predicted_fraction = 0.01
calibration_min_predicted_seconds = 300.0
calibration_sample_seconds = 10.0
calibration_warmup_seconds = 1.0
calibration_max_candidates = 8
calibration_min_gain_fraction = 0.03

[scanner]
roots = []
extensions = [".mkv", ".mp4", ".m4v", ".mov", ".avi", ".webm", ".ts", ".m2ts"]
exclude_directories = [".avarch"]

[profile_registry]
search_paths = ["profiles"]
"""


def load_config(path: Path) -> AppConfig:
    with path.open("rb") as config_file:
        data = tomllib.load(config_file)

    config = AppConfig.model_validate(data)
    return _resolve_profile_search_paths(config, path.parent)


def resolve_data_dir(config: AppConfig, config_path: Path) -> Path:
    data_dir = config.app.data_dir
    if data_dir.is_absolute():
        return data_dir

    return config_path.parent / data_dir


def _resolve_profile_search_paths(config: AppConfig, config_dir: Path) -> AppConfig:
    search_paths = [
        path if path.is_absolute() else config_dir / path
        for path in config.profile_registry.search_paths
    ]
    return config.model_copy(
        update={
            "profile_registry": config.profile_registry.model_copy(
                update={"search_paths": search_paths}
            )
        }
    )
