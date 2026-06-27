from pathlib import Path

from avarch.adapters.sqlite.urls import resolve_database_url
from avarch.config import AppConfig, DatabaseSettings


def test_relative_sqlite_database_url_resolves_from_config_file() -> None:
    config = AppConfig(database=DatabaseSettings(url="sqlite:///data/avarch.db"))
    config_path = Path("/workspace/.avarch/config.toml")

    assert (
        resolve_database_url(config, config_path) == "sqlite:////workspace/.avarch/data/avarch.db"
    )


def test_absolute_sqlite_database_url_is_preserved() -> None:
    config = AppConfig(database=DatabaseSettings(url="sqlite:////var/lib/avarch/avarch.db"))

    assert (
        resolve_database_url(config, Path("/workspace/.avarch/config.toml"))
        == "sqlite:////var/lib/avarch/avarch.db"
    )


def test_memory_sqlite_database_url_is_preserved() -> None:
    config = AppConfig(database=DatabaseSettings(url="sqlite:///:memory:"))

    assert (
        resolve_database_url(config, Path("/workspace/.avarch/config.toml")) == "sqlite:///:memory:"
    )


def test_non_sqlite_database_url_is_preserved() -> None:
    config = AppConfig(database=DatabaseSettings(url="postgresql://localhost/avarch"))

    assert (
        resolve_database_url(config, Path("/workspace/.avarch/config.toml"))
        == "postgresql://localhost/avarch"
    )
