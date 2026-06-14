from __future__ import annotations

import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.engine import make_url

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
LogFormat = Literal["console", "json"]


class AppSettings(BaseModel):
    data_dir: Path = Path(".avarch")


class DatabaseSettings(BaseModel):
    url: str = "sqlite:///.avarch/avarch.db"


class LoggingSettings(BaseModel):
    level: LogLevel = "INFO"
    format: LogFormat = "console"


class ProfileMatchSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_codec_not: list[str] = Field(default_factory=list)

    @field_validator("video_codec_not", mode="before")
    @classmethod
    def normalize_video_codecs(cls, value: object) -> list[str]:
        return _normalized_text_list(value)


class ProfileVideoSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_width: int = Field(gt=0)
    hdr_to_sdr: bool = False
    source: Literal["vapoursynth"] = "vapoursynth"


class ProfileAv1anSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    encoder: str
    workers: int = Field(gt=0)
    video_args: str


class ProfileAudioSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    codec: str
    bitrate: str
    channels: int = Field(gt=0)
    languages: list[str]

    @field_validator("codec", mode="before")
    @classmethod
    def normalize_codec(cls, value: object) -> str:
        return str(value).strip().lower()

    @field_validator("languages", mode="before")
    @classmethod
    def normalize_languages(cls, value: object) -> list[str]:
        return _dedupe_preserving_order(_normalized_text_list(value))


class ProfileSubtitleSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    languages: list[str]
    keep_forced: bool = True

    @field_validator("languages", mode="before")
    @classmethod
    def normalize_languages(cls, value: object) -> list[str]:
        return _dedupe_preserving_order(_normalized_text_list(value))


class EncodingProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: Literal["av1an"]
    container: Literal["mkv"]
    match: ProfileMatchSettings
    video: ProfileVideoSettings
    av1an: ProfileAv1anSettings
    audio: ProfileAudioSettings
    subtitles: ProfileSubtitleSettings


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
        ".avarch-work",
        ".avarch-output",
    }


class ScannerSettings(BaseModel):
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


def _normalized_text_list(value: object) -> list[str]:
    if value is None:
        return []

    values = [value] if isinstance(value, str) else _iterable_values(value)
    return [str(item).strip().lower() for item in values]


def _dedupe_preserving_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


class AppConfig(BaseModel):
    app: AppSettings = AppSettings()
    database: DatabaseSettings = DatabaseSettings()
    logging: LoggingSettings = LoggingSettings()
    scanner: ScannerSettings = ScannerSettings()
    profiles: dict[str, EncodingProfile] = Field(default_factory=dict)


DEFAULT_CONFIG_TEXT = """[app]
data_dir = ".avarch"

[database]
url = "sqlite:///.avarch/avarch.db"

[logging]
level = "INFO"
format = "console"

[scanner]
roots = []
extensions = [".mkv", ".mp4", ".m4v", ".mov", ".avi", ".webm", ".ts", ".m2ts"]
exclude_directories = [".avarch", ".avarch-work", ".avarch-output"]

[profiles.av1_1080p_sdr]
backend = "av1an"
container = "mkv"

[profiles.av1_1080p_sdr.match]
video_codec_not = ["av1"]

[profiles.av1_1080p_sdr.video]
max_width = 1920
hdr_to_sdr = true
source = "vapoursynth"

[profiles.av1_1080p_sdr.av1an]
encoder = "svt-av1"
workers = 6
video_args = "--preset 6 --crf 28 --keyint 240"

[profiles.av1_1080p_sdr.audio]
codec = "libopus"
bitrate = "128k"
channels = 2
languages = ["eng"]

[profiles.av1_1080p_sdr.subtitles]
languages = ["eng"]
keep_forced = true
"""


def load_config(path: Path) -> AppConfig:
    with path.open("rb") as config_file:
        data = tomllib.load(config_file)

    return AppConfig.model_validate(data)


def resolve_data_dir(config: AppConfig, config_path: Path) -> Path:
    data_dir = config.app.data_dir
    if data_dir.is_absolute():
        return data_dir

    return config_path.parent / data_dir


def resolve_database_url(config: AppConfig, config_path: Path) -> str:
    url = make_url(config.database.url)
    database = url.database
    if url.drivername != "sqlite" or database is None or database in {"", ":memory:"}:
        return config.database.url

    db_path = Path(database)
    if not db_path.is_absolute():
        db_path = config_path.parent / db_path

    return url.set(database=str(db_path)).render_as_string(hide_password=False)
