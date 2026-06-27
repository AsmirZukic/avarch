from pathlib import Path

import pytest
from pydantic import ValidationError

from avarch.config import WORKSPACE_CONFIG_TEXT, AppConfig, load_config


def test_default_config_has_sqlite_database() -> None:
    config = AppConfig()

    assert config.database.url == "sqlite:///data/avarch.adapters.sqlite.db"


def test_default_config_uses_workspace_data_dir() -> None:
    config = AppConfig()

    assert config.app.data_dir == Path("data")


def test_default_config_has_console_logging() -> None:
    config = AppConfig()

    assert config.logging.level == "INFO"
    assert config.logging.format == "console"


def test_default_resource_limits() -> None:
    config = AppConfig()

    assert config.resources.cheap_workers == 4
    assert config.resources.av1an_jobs == 1
    assert config.resources.file_ops == 1


def test_load_config_from_toml(tmp_path: Path) -> None:
    config_file = tmp_path / ".avarch" / "config.toml"
    config_file.parent.mkdir()
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


def test_config_loads_resource_limits(tmp_path: Path) -> None:
    config_file = tmp_path / ".avarch" / "config.toml"
    config_file.parent.mkdir()
    config_file.write_text(
        """
[resources]
cheap_workers = 8
av1an_jobs = 2
file_ops = 3
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_file)

    assert config.resources.cheap_workers == 8
    assert config.resources.av1an_jobs == 2
    assert config.resources.file_ops == 3


def test_resource_limits_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"resources": {"cheap_workers": 0}})


def test_existing_config_without_resources_still_loads(tmp_path: Path) -> None:
    config_file = tmp_path / ".avarch" / "config.toml"
    config_file.parent.mkdir()
    config_file.write_text("[scanner]\nroots = []\n", encoding="utf-8")

    config = load_config(config_file)

    assert config.resources.cheap_workers == 4


def test_resource_config_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"resources": {"surprise": 1}})


def test_unknown_top_level_configuration_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"old_profiles": {}})


def test_unknown_app_configuration_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"app": {"work_dir": "work"}})


def test_unknown_scanner_configuration_field_is_rejected() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"scanner": {"directory_excludes": []}})


def test_default_scanner_config_has_no_roots() -> None:
    config = AppConfig()

    assert config.scanner.roots == []


def test_load_scanner_config(tmp_path: Path) -> None:
    config_file = tmp_path / ".avarch" / "config.toml"
    config_file.parent.mkdir()
    config_file.write_text(
        """
[scanner]
roots = ["/storage/media"]
extensions = [".mkv", "MP4", ".mp4"]
exclude_directories = ["transient", "tmp"]
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_file)

    assert config.scanner.roots == [Path("/storage/media")]
    assert config.scanner.extensions == {".mkv", ".mp4"}
    assert config.scanner.exclude_directories == {"transient", "tmp"}


def test_scanner_extensions_are_normalized() -> None:
    config = AppConfig.model_validate({"scanner": {"extensions": ["MKV", ".Mp4", ".mp4"]}})

    assert config.scanner.extensions == {".mkv", ".mp4"}


def test_workspace_config_text_includes_scanner_settings() -> None:
    assert "[scanner]" in WORKSPACE_CONFIG_TEXT
    assert "exclude_directories" in WORKSPACE_CONFIG_TEXT


def test_workspace_config_text_includes_profile_registry_settings() -> None:
    assert "[profile_registry]" in WORKSPACE_CONFIG_TEXT
    assert "search_paths" in WORKSPACE_CONFIG_TEXT
    assert "[profiles." not in WORKSPACE_CONFIG_TEXT


def test_app_config_contains_profile_registry_settings() -> None:
    config = AppConfig()

    assert config.profile_registry.search_paths == [Path("profiles")]


def test_load_config_resolves_profile_registry_search_paths(tmp_path: Path) -> None:
    config_file = tmp_path / ".avarch" / "config.toml"
    config_file.parent.mkdir()
    config_file.write_text(
        """
[profile_registry]
search_paths = ["profiles", "/opt/avarch/profiles"]
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_file)

    assert config.profile_registry.search_paths == [
        tmp_path / ".avarch" / "profiles",
        Path("/opt/avarch/profiles"),
    ]


def test_app_config_contains_no_profile_documents() -> None:
    config = AppConfig()

    assert not hasattr(config, "profiles")


def test_profile_documents_are_not_root_configuration() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"profiles": {}})


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
