from pathlib import Path

import pytest
from pydantic import ValidationError

from avarch.config import AppConfig, load_config


def test_default_config_has_sqlite_database() -> None:
    config = AppConfig()

    assert config.database.url.startswith("sqlite:///")


def test_default_config_has_console_logging() -> None:
    config = AppConfig()

    assert config.logging.level == "INFO"
    assert config.logging.format == "console"


def test_load_config_from_toml(tmp_path: Path) -> None:
    config_file = tmp_path / "avarch.toml"
    config_file.write_text(
        """
[app]
data_dir = "custom-data"

[database]
url = "sqlite:///custom-data/custom.db"

[logging]
level = "DEBUG"
format = "json"
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_file)

    assert config.app.data_dir == Path("custom-data")
    assert config.database.url == "sqlite:///custom-data/custom.db"
    assert config.logging.level == "DEBUG"
    assert config.logging.format == "json"


def test_invalid_logging_level_fails() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                "logging": {
                    "level": "TRACE",
                    "format": "console",
                }
            }
        )


def test_invalid_logging_format_fails() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {
                "logging": {
                    "level": "INFO",
                    "format": "xml",
                }
            }
        )
