from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from avarch.config import DEFAULT_CONFIG_TEXT, AppConfig, load_config


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


def test_default_scanner_config_has_no_roots() -> None:
    config = AppConfig()

    assert config.scanner.roots == []


def test_load_scanner_config(tmp_path: Path) -> None:
    config_file = tmp_path / "avarch.toml"
    config_file.write_text(
        """
[scanner]
roots = ["/storage/media"]
extensions = [".mkv", "MP4", ".mp4"]
exclude_directories = [".avarch-work", "tmp"]
""".strip(),
        encoding="utf-8",
    )

    config = load_config(config_file)

    assert config.scanner.roots == [Path("/storage/media")]
    assert config.scanner.extensions == {".mkv", ".mp4"}
    assert config.scanner.exclude_directories == {".avarch-work", "tmp"}


def test_scanner_extensions_are_normalized() -> None:
    config = AppConfig.model_validate(
        {"scanner": {"extensions": ["MKV", ".Mp4", ".mp4"]}}
    )

    assert config.scanner.extensions == {".mkv", ".mp4"}


def test_default_config_text_includes_scanner_settings() -> None:
    assert "[scanner]" in DEFAULT_CONFIG_TEXT
    assert "exclude_directories" in DEFAULT_CONFIG_TEXT


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


def test_config_loads_profile(tmp_path: Path) -> None:
    config_file = tmp_path / "avarch.toml"
    config_file.write_text(_profile_config_text(), encoding="utf-8")

    config = load_config(config_file)
    profile = config.profiles["av1_1080p_sdr"]

    assert profile.backend == "av1an"
    assert profile.container == "mkv"
    assert profile.video.max_width == 1920
    assert profile.audio.codec == "libopus"


def test_profile_accepts_optional_vapoursynth_template(tmp_path: Path) -> None:
    template = tmp_path / "templates" / "custom.vpy"
    config_file = tmp_path / "avarch.toml"
    config_file.write_text(
        _profile_config_text().replace(
            'container = "mkv"',
            'container = "mkv"\nvapoursynth_template = "templates/custom.vpy"',
        ),
        encoding="utf-8",
    )

    config = load_config(config_file)

    assert config.profiles["av1_1080p_sdr"].vapoursynth_template == template


def test_config_without_profiles_remains_valid() -> None:
    config = AppConfig.model_validate({"scanner": {"roots": []}})

    assert config.profiles == {}


def test_profile_normalizes_codec_names() -> None:
    config = AppConfig.model_validate(
        {
            "profiles": {
                "test": _profile_data(
                    match={"video_codec_not": [" AV1 ", "HeVc"]},
                    audio={"codec": " LIBOPUS "},
                )
            }
        }
    )

    profile = config.profiles["test"]
    assert profile.match.video_codec_not == ["av1", "hevc"]
    assert profile.audio.codec == "libopus"


def test_profile_preserves_language_priority() -> None:
    config = AppConfig.model_validate(
        {
            "profiles": {
                "test": _profile_data(
                    audio={"languages": [" JPN ", "eng", "jpn"]},
                    subtitles={"languages": ["ENG", "jpn", "eng"]},
                )
            }
        }
    )

    profile = config.profiles["test"]
    assert profile.audio.languages == ["jpn", "eng"]
    assert profile.subtitles.languages == ["eng", "jpn"]


def test_profile_rejects_nonpositive_workers() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {"profiles": {"test": _profile_data(av1an={"workers": 0})}}
        )


def test_profile_rejects_nonpositive_max_width() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {"profiles": {"test": _profile_data(video={"max_width": 0})}}
        )


def test_profile_requires_even_max_width() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {"profiles": {"test": _profile_data(video={"max_width": 1919})}}
        )


def test_profile_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate(
            {"profiles": {"test": _profile_data(video={"surprise": True})}}
        )


def _profile_config_text() -> str:
    return """
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
""".strip()


def _profile_data(
    *,
    match: dict[str, object] | None = None,
    video: dict[str, object] | None = None,
    av1an: dict[str, object] | None = None,
    audio: dict[str, object] | None = None,
    subtitles: dict[str, object] | None = None,
) -> dict[str, object]:
    profile: dict[str, object] = {
        "backend": "av1an",
        "container": "mkv",
        "match": {"video_codec_not": ["av1"]},
        "video": {
            "max_width": 1920,
            "hdr_to_sdr": True,
            "source": "vapoursynth",
        },
        "av1an": {
            "encoder": "svt-av1",
            "workers": 6,
            "video_args": "--preset 6 --crf 28 --keyint 240",
        },
        "audio": {
            "codec": "libopus",
            "bitrate": "128k",
            "channels": 2,
            "languages": ["eng"],
        },
        "subtitles": {
            "languages": ["eng"],
            "keep_forced": True,
        },
    }
    for key, override in {
        "match": match,
        "video": video,
        "av1an": av1an,
        "audio": audio,
        "subtitles": subtitles,
    }.items():
        if override is not None:
            value = dict(cast(dict[str, object], profile[key]))
            value.update(override)
            profile[key] = value
    return profile
