from __future__ import annotations

from avarch.profiles.models import (
    EncodingProfile,
    ProfileAudioSettings,
    ProfileAv1anSettings,
    ProfileMatchSettings,
    ProfilePromotionSettings,
    ProfileSubtitleSettings,
    ProfileVideoSettings,
)
from avarch.size_policy import SizeDecision, evaluate_size_policy


def test_rejects_larger_output() -> None:
    assert evaluate_size_policy(1000, 1001, _profile()) == SizeDecision.REJECT_NOT_SMALLER


def test_rejects_same_size_output() -> None:
    assert evaluate_size_policy(1000, 1000, _profile()) == SizeDecision.REJECT_NOT_SMALLER


def test_rejects_output_below_minimum_savings() -> None:
    assert (
        evaluate_size_policy(1000, 999, _profile())
        == SizeDecision.REJECT_MINIMUM_SAVINGS_NOT_MET
    )


def test_accepts_output_above_minimum_savings() -> None:
    assert evaluate_size_policy(1000, 900, _profile()) == SizeDecision.ACCEPT


def test_allows_zero_minimum_savings_when_configured() -> None:
    assert (
        evaluate_size_policy(
            1000,
            999,
            _profile(promotion=ProfilePromotionSettings(minimum_savings_percent=0)),
        )
        == SizeDecision.ACCEPT
    )


def _profile(*, promotion: ProfilePromotionSettings | None = None) -> EncodingProfile:
    return EncodingProfile(
        backend="av1an",
        container="mkv",
        match=ProfileMatchSettings(video_codec_not=["av1"]),
        video=ProfileVideoSettings(max_width=1920),
        av1an=ProfileAv1anSettings(encoder="svt-av1", workers=1, video_args="--crf 30"),
        audio=ProfileAudioSettings(codec="opus", bitrate="128k", channels=2, languages=["eng"]),
        subtitles=ProfileSubtitleSettings(languages=["eng"]),
        promotion=promotion or ProfilePromotionSettings(),
    )