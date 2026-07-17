from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from statistics import median


@dataclass(frozen=True, slots=True)
class ComparableEncodeKey:
    profile_name: str
    resolution_class: str
    bit_depth: int | None
    encoder: str | None
    preset: str | None
    vapoursynth_identity: str | None
    workers: int | None


@dataclass(frozen=True, slots=True)
class ComparableEncodeSample:
    job_id: int
    attempt_id: int
    key: ComparableEncodeKey
    duration_seconds: int
    finished_at: datetime


class ForecastConfidence(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class QueueDurationEstimate:
    available: bool
    reason: str | None
    confidence: ForecastConfidence
    sample_count: int
    lower_seconds: int | None = None
    median_seconds: int | None = None
    upper_seconds: int | None = None


def estimate_encode_duration(
    *,
    history: tuple[ComparableEncodeSample, ...],
    key: ComparableEncodeKey,
    av1an_jobs: int,
    source_frame_count: int | None = None,
) -> QueueDurationEstimate:
    del source_frame_count
    if av1an_jobs != 1:
        return _unavailable("multi_lane_capacity_not_supported")
    durations = sorted(
        sample.duration_seconds
        for sample in history
        if sample.key == key and sample.duration_seconds > 0
    )
    if len(durations) < 5:
        return _unavailable("insufficient_history", sample_count=len(durations))
    return QueueDurationEstimate(
        available=True,
        reason=None,
        confidence=ForecastConfidence.LOW if len(durations) < 10 else ForecastConfidence.MEDIUM,
        sample_count=len(durations),
        lower_seconds=durations[0],
        median_seconds=int(median(durations)),
        upper_seconds=durations[-1],
    )


def estimate_queue_duration(
    *,
    history: tuple[ComparableEncodeSample, ...],
    queued_keys: tuple[ComparableEncodeKey, ...],
    av1an_jobs: int,
) -> QueueDurationEstimate:
    if av1an_jobs != 1:
        return _unavailable("multi_lane_capacity_not_supported")
    estimates = tuple(
        estimate_encode_duration(history=history, key=key, av1an_jobs=av1an_jobs)
        for key in queued_keys
    )
    unavailable = next((estimate for estimate in estimates if not estimate.available), None)
    if unavailable is not None:
        return unavailable
    if not estimates:
        return _unavailable("empty_queue")
    return QueueDurationEstimate(
        available=True,
        reason=None,
        confidence=(
            ForecastConfidence.LOW
            if any(estimate.confidence == ForecastConfidence.LOW for estimate in estimates)
            else ForecastConfidence.MEDIUM
        ),
        sample_count=min(estimate.sample_count for estimate in estimates),
        lower_seconds=sum(estimate.lower_seconds or 0 for estimate in estimates),
        median_seconds=sum(estimate.median_seconds or 0 for estimate in estimates),
        upper_seconds=sum(estimate.upper_seconds or 0 for estimate in estimates),
    )


def _unavailable(reason: str, *, sample_count: int = 0) -> QueueDurationEstimate:
    return QueueDurationEstimate(
        available=False,
        reason=reason,
        confidence=ForecastConfidence.UNAVAILABLE,
        sample_count=sample_count,
    )
