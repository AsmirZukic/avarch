from typing import cast

import pytest

from avarch.planner import build_profile_hash, parse_encoder_args
from avarch.profiles.models import EncodingProfile


def test_parse_encoder_args() -> None:
    assert parse_encoder_args("--preset 6 --crf 28 --keyint 240") == [
        "--preset",
        "6",
        "--crf",
        "28",
        "--keyint",
        "240",
    ]


def test_parse_encoder_args_preserves_quoted_value() -> None:
    assert parse_encoder_args('--tune "subjective ssim" --crf 28') == [
        "--tune",
        "subjective ssim",
        "--crf",
        "28",
    ]


def test_parse_encoder_args_rejects_invalid_quoting() -> None:
    with pytest.raises(ValueError):
        parse_encoder_args('--tune "subjective')


def test_profile_hash_is_deterministic() -> None:
    profile = _profile()

    assert build_profile_hash(profile) == build_profile_hash(profile)


def test_profile_hash_ignores_toml_key_order() -> None:
    left = _profile(
        {
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
                "video_args": "--preset 6 --crf 28",
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
    )
    right = _profile(
        {
            "subtitles": {
                "keep_forced": True,
                "languages": ["eng"],
            },
            "audio": {
                "languages": ["eng"],
                "channels": 2,
                "bitrate": "128k",
                "codec": "libopus",
            },
            "av1an": {
                "video_args": "--preset 6 --crf 28",
                "workers": 6,
                "encoder": "svt-av1",
            },
            "video": {
                "source": "vapoursynth",
                "hdr_to_sdr": True,
                "max_width": 1920,
            },
            "match": {"video_codec_not": ["av1"]},
            "container": "mkv",
            "backend": "av1an",
        }
    )

    assert build_profile_hash(left) == build_profile_hash(right)


def test_profile_hash_changes_when_encoder_args_change() -> None:
    left = _profile({"av1an": {"video_args": "--preset 6 --crf 28"}})
    right = _profile({"av1an": {"video_args": "--preset 6 --crf 30"}})

    assert build_profile_hash(left) != build_profile_hash(right)


def test_profile_hash_changes_when_stream_policy_changes() -> None:
    left = _profile({"audio": {"languages": ["eng"]}})
    right = _profile({"audio": {"languages": ["jpn", "eng"]}})

    assert build_profile_hash(left) != build_profile_hash(right)


def test_same_template_content_at_different_paths_has_same_profile_hash() -> None:
    left = _profile({"vapoursynth_template": "/templates/left.vpy"})
    right = _profile({"vapoursynth_template": "/templates/right.vpy"})

    assert build_profile_hash(left, template_hash="template-hash") == build_profile_hash(
        right,
        template_hash="template-hash",
    )


def test_changed_template_content_changes_profile_hash() -> None:
    profile = _profile({"vapoursynth_template": "/templates/custom.vpy"})

    assert build_profile_hash(profile, template_hash="first") != build_profile_hash(
        profile,
        template_hash="second",
    )


def test_changed_filter_content_changes_profile_hash() -> None:
    profile = _profile(
        {
            "vapoursynth": {
                "mode": "custom_filter",
                "script": "/scripts/filter.py",
                "entrypoint": "apply",
                "api_version": 1,
            }
        }
    )

    assert build_profile_hash(profile, script_hash="first") != build_profile_hash(
        profile,
        script_hash="second",
    )


def _profile(overrides: dict[str, object] | None = None) -> EncodingProfile:
    profile = _profile_data()
    if overrides:
        for key, value in overrides.items():
            if isinstance(value, dict) and isinstance(profile.get(key), dict):
                section = dict(cast(dict[str, object], profile[key]))
                section.update(cast(dict[str, object], value))
                profile[key] = section
            else:
                profile[key] = value

    return EncodingProfile.model_validate(profile)


def _profile_data() -> dict[str, object]:
    return {
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
            "video_args": "--preset 6 --crf 28",
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
