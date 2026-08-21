from __future__ import annotations

from avarch.domain.profiles import (
    ProfileMatchFacts,
    ProfileMatchPolicy,
    evaluate_profile_match,
)


def test_profile_match_accepts_allowed_codec() -> None:
    result = evaluate_profile_match(_facts("hevc"), _policy("av1"))

    assert result.matched is True
    assert result.reasons == ()


def test_profile_match_rejects_missing_video_stream() -> None:
    result = evaluate_profile_match(
        ProfileMatchFacts(has_video_stream=False, primary_video_codec=None),
        _policy("av1"),
    )

    assert result.matched is False
    assert result.reasons == ("no video stream",)


def test_profile_match_rejects_missing_primary_codec() -> None:
    result = evaluate_profile_match(_facts(None), _policy("av1"))

    assert result.matched is False
    assert result.reasons == ("primary video codec is missing",)


def test_profile_match_rejects_excluded_codec_case_insensitively() -> None:
    result = evaluate_profile_match(_facts("AV1"), _policy("av1"))

    assert result.matched is False
    assert result.reasons == ("video codec is excluded: av1",)


def _facts(primary_video_codec: str | None) -> ProfileMatchFacts:
    return ProfileMatchFacts(
        has_video_stream=True,
        primary_video_codec=primary_video_codec,
    )


def _policy(*excluded_codecs: str) -> ProfileMatchPolicy:
    return ProfileMatchPolicy(excluded_video_codecs=frozenset(excluded_codecs))
