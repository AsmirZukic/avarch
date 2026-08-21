from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from avarch.config import AppConfig
from avarch.profiles.models import ProfileDocument
from avarch.profiles.registry import (
    DuplicateProfileNameError,
    ProfileOrigin,
    ProfileRegistry,
)


def test_user_profiles_load_through_registry(tmp_path: Path) -> None:
    profiles_dir = tmp_path / ".avarch" / "profiles"
    profiles_dir.mkdir(parents=True)
    profile_path = profiles_dir / "my_1080p.toml"
    profile_path.write_text(
        _profile_text(
            name="my_1080p",
            extra='[vapoursynth]\nmode = "custom_template"\ntemplate = "custom.vpy"\n',
        ),
        encoding="utf-8",
    )

    registry = ProfileRegistry.from_config(_config(profiles_dir))
    profile = registry.get("my_1080p")

    assert profile.origin == ProfileOrigin.USER
    assert profile.profile.video.max_width == 1920
    assert profile.profile.vapoursynth.template == tmp_path / ".avarch" / "scripts" / "custom.vpy"


def test_user_profiles_load_recursively(tmp_path: Path) -> None:
    nested = tmp_path / "series" / "animation"
    nested.mkdir(parents=True)
    profile_path = nested / "my_anime.toml"
    profile_path.write_text(
        _profile_text(name="my_anime"),
        encoding="utf-8",
    )

    registry = ProfileRegistry.from_config(_config(tmp_path))

    assert registry.get("my_anime").source == str(profile_path)


def test_duplicate_profile_names_fail(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "copy.toml").write_text(
        _profile_text(name="shared"),
        encoding="utf-8",
    )
    (second / "other.toml").write_text(
        _profile_text(name="shared"),
        encoding="utf-8",
    )

    with pytest.raises(DuplicateProfileNameError):
        ProfileRegistry.from_config(_config(first, second))


def test_profile_document_normalizes_codec_names() -> None:
    profile = _profile_document(
        match={"video_codec_not": [" AV1 ", "HeVc"]},
        audio={"codec": " LIBOPUS "},
    )

    assert profile.match.video_codec_not == ["av1", "hevc"]
    assert profile.audio.codec == "libopus"


def test_profile_document_preserves_language_priority() -> None:
    profile = _profile_document(
        audio={"languages": [" JPN ", "eng", "jpn"]},
        subtitles={"languages": ["ENG", "jpn", "eng"]},
    )

    assert profile.audio.languages == ["jpn", "eng"]
    assert profile.subtitles.languages == ["eng", "jpn"]


def test_profile_document_rejects_nonpositive_workers() -> None:
    with pytest.raises(ValidationError):
        _profile_document(av1an={"workers": 0})


@pytest.mark.parametrize("workers", [-1, True, False, "0", "bogus"])
def test_profile_document_rejects_invalid_worker_values(workers: object) -> None:
    with pytest.raises(ValidationError):
        _profile_document(av1an={"workers": workers})


def test_profile_document_accepts_auto_workers() -> None:
    profile = _profile_document(av1an={"workers": "auto"})

    assert profile.av1an.workers == "auto"


def test_profile_document_rejects_raw_svt_lp() -> None:
    with pytest.raises(ValidationError, match="must be configured with svt_lp"):
        _profile_document(av1an={"video_args": "--preset 6 --lp 4"})


def test_profile_document_rejects_nonpositive_max_width() -> None:
    with pytest.raises(ValidationError):
        _profile_document(video={"max_width": 0})


def test_profile_document_requires_even_max_width() -> None:
    with pytest.raises(ValidationError):
        _profile_document(video={"max_width": 1919})


def test_profile_document_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        _profile_document(video={"surprise": True})


def test_builtin_profiles_have_no_python_definitions() -> None:
    source_root = Path(__file__).parents[1] / "src" / "avarch"
    python_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in source_root.rglob("*.py")
        if "__pycache__" not in path.parts
    )

    assert '"av1_1080p_sdr": {' not in python_sources
    assert "[profiles.av1_1080p_sdr]" not in python_sources


def _profile_document(
    *,
    match: dict[str, object] | None = None,
    video: dict[str, object] | None = None,
    av1an: dict[str, object] | None = None,
    audio: dict[str, object] | None = None,
    subtitles: dict[str, object] | None = None,
) -> ProfileDocument:
    return ProfileDocument.model_validate(
        _profile_data(
            match=match,
            video=video,
            av1an=av1an,
            audio=audio,
            subtitles=subtitles,
        )
    )


def _profile_data(
    *,
    name: str = "test",
    match: dict[str, object] | None = None,
    video: dict[str, object] | None = None,
    av1an: dict[str, object] | None = None,
    audio: dict[str, object] | None = None,
    subtitles: dict[str, object] | None = None,
) -> dict[str, object]:
    profile: dict[str, object] = {
        "schema_version": 1,
        "name": name,
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


def _profile_text(*, name: str, extra: str = "") -> str:
    return f"""
schema_version = 1
name = "{name}"

backend = "av1an"
container = "mkv"
{extra}
[match]
video_codec_not = ["av1"]

[video]
max_width = 1920
hdr_to_sdr = true
source = "vapoursynth"

[av1an]
encoder = "svt-av1"
workers = 6
video_args = "--preset 6 --crf 28 --keyint 240"

[audio]
codec = "libopus"
bitrate = "128k"
channels = 2
languages = ["eng"]

[subtitles]
languages = ["eng"]
keep_forced = true
""".strip()


def _config(*search_paths: Path) -> AppConfig:
    return AppConfig.model_validate({"profile_registry": {"search_paths": list(search_paths)}})
