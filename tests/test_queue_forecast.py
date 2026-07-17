from __future__ import annotations

from datetime import UTC, datetime, timedelta

from avarch.application.queue_forecast import (
    ComparableEncodeKey,
    ComparableEncodeSample,
    ForecastConfidence,
    estimate_encode_duration,
    estimate_queue_duration,
)


def test_fewer_than_five_samples_returns_unavailable() -> None:
    estimate = estimate_encode_duration(
        history=_samples([100, 110, 120, 130]),
        key=_KEY,
        av1an_jobs=1,
    )

    assert estimate.available is False
    assert estimate.reason == "insufficient_history"
    assert estimate.sample_count == 4


def test_five_to_nine_samples_returns_low_confidence() -> None:
    estimate = estimate_encode_duration(
        history=_samples([100, 110, 120, 130, 140]),
        key=_KEY,
        av1an_jobs=1,
    )

    assert estimate.available is True
    assert estimate.confidence == ForecastConfidence.LOW
    assert estimate.sample_count == 5


def test_estimate_uses_median_duration() -> None:
    estimate = estimate_encode_duration(
        history=_samples([100, 100, 120, 140, 500]),
        key=_KEY,
        av1an_jobs=1,
    )

    assert estimate.median_seconds == 120


def test_outliers_widen_range_without_shifting_center_wildly() -> None:
    estimate = estimate_encode_duration(
        history=_samples([100, 101, 102, 103, 1000]),
        key=_KEY,
        av1an_jobs=1,
    )

    assert estimate.median_seconds == 102
    assert estimate.lower_seconds == 100
    assert estimate.upper_seconds == 1000


def test_missing_frame_count_falls_back_only_to_duration_model_with_history() -> None:
    missing = estimate_encode_duration(
        history=(),
        key=_KEY,
        av1an_jobs=1,
        source_frame_count=None,
    )
    available = estimate_encode_duration(
        history=_samples([100, 110, 120, 130, 140]),
        key=_KEY,
        av1an_jobs=1,
        source_frame_count=None,
    )

    assert missing.available is False
    assert available.available is True


def test_capacity_greater_than_one_is_unavailable_initially() -> None:
    estimate = estimate_encode_duration(
        history=_samples([100, 110, 120, 130, 140]),
        key=_KEY,
        av1an_jobs=2,
    )

    assert estimate.available is False
    assert estimate.reason == "multi_lane_capacity_not_supported"


def test_queue_duration_keeps_encode_estimate_separate_from_other_stages() -> None:
    estimate = estimate_queue_duration(
        history=_samples([100, 110, 120, 130, 140]),
        queued_keys=(_KEY,),
        av1an_jobs=1,
    )

    assert estimate.available is True
    assert estimate.median_seconds == 120


_KEY = ComparableEncodeKey(
    profile_name="default",
    resolution_class="1080p",
    bit_depth=10,
    encoder="svt-av1",
    preset="6",
    vapoursynth_identity="execution",
    workers=2,
)


def _samples(durations: list[int]) -> tuple[ComparableEncodeSample, ...]:
    finished_at = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    return tuple(
        ComparableEncodeSample(
            job_id=index,
            attempt_id=index,
            key=_KEY,
            duration_seconds=duration,
            finished_at=finished_at - timedelta(minutes=index),
        )
        for index, duration in enumerate(durations, start=1)
    )
