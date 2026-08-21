from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProfileMatchFacts:
    has_video_stream: bool
    primary_video_codec: str | None


@dataclass(frozen=True, slots=True)
class ProfileMatchPolicy:
    excluded_video_codecs: frozenset[str]


@dataclass(frozen=True, slots=True)
class ProfileMatchResult:
    matched: bool
    reasons: tuple[str, ...]


def evaluate_profile_match(
    facts: ProfileMatchFacts,
    policy: ProfileMatchPolicy,
) -> ProfileMatchResult:
    if not facts.has_video_stream:
        return ProfileMatchResult(False, ("no video stream",))

    if facts.primary_video_codec is None:
        return ProfileMatchResult(False, ("primary video codec is missing",))

    source_codec = facts.primary_video_codec.strip().lower()
    blocked_codecs = {codec.strip().lower() for codec in policy.excluded_video_codecs}
    if source_codec in blocked_codecs:
        return ProfileMatchResult(False, (f"video codec is excluded: {source_codec}",))

    return ProfileMatchResult(True, ())
