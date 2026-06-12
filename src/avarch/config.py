from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel
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


class AppConfig(BaseModel):
    app: AppSettings = AppSettings()
    database: DatabaseSettings = DatabaseSettings()
    logging: LoggingSettings = LoggingSettings()


DEFAULT_CONFIG_TEXT = """[app]
data_dir = ".avarch"

[database]
url = "sqlite:///.avarch/avarch.db"

[logging]
level = "INFO"
format = "console"
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
