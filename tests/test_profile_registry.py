from pathlib import Path
from typing import cast

import pytest
from pydantic import ValidationError

from avarch.profiles.models import ProfileDocument
from avarch.profiles.registry import (
    DuplicateProfileNameError,
    ProfileOrigin,
    ProfileRegistry,
    ProfileRegistryError,
    seed_packaged_profiles,
)


def test_registry_loads_seeded_profiles(tmp_path: Path) -> None:
    seed_packaged_profiles(tmp_path)
    registry = ProfileRegistry.load(search_paths=[tmp_path])
    profile = registry.get("av1_1080p_sdr")

    assert profile.origin == ProfileOrigin.USER
    assert profile.profile.backend == "av1an"
    assert profile.profile.container == "mkv"


def test_user_profiles_load_through_registry(tmp_path: Path) -> None:
    profile_path = tmp_path / "my_1080p.toml"
    profile_path.write_text(
        _profile_text(name="my_1080p", extra='vapoursynth_template = "custom.vpy"\n'),
        encoding="utf-8",
    )

    registry = ProfileRegistry.load(search_paths=[tmp_path])
    profile = registry.get("my_1080p")

    assert profile.origin == ProfileOrigin.USER
    assert profile.profile.video.max_width == 1920
    assert profile.profile.vapoursynth_template == tmp_path / "custom.vpy"


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
        ProfileRegistry.load(search_paths=[first, second])


def test_validate_all_reports_loaded_profiles(tmp_path: Path) -> None:
    seed_packaged_profiles(tmp_path)
    summary = ProfileRegistry.load(search_paths=[tmp_path]).validate_all()

    assert summary.ok is True
    assert [profile.name for profile in summary.valid] == ["av1_1080p_sdr"]


def test_registry_fails_loudly_when_no_profile_documents_exist(tmp_path: Path) -> None:
    with pytest.raises(ProfileRegistryError, match="No profiles found"):
        ProfileRegistry.load(search_paths=[tmp_path])


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
