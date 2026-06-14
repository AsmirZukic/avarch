from __future__ import annotations

import tomllib
from collections.abc import Iterable
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, Field, field_validator
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


class AppConfig(BaseModel):
    app: AppSettings = AppSettings()
    database: DatabaseSettings = DatabaseSettings()
    logging: LoggingSettings = LoggingSettings()
    scanner: ScannerSettings = ScannerSettings()


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
