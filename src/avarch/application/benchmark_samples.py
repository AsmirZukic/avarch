from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from enum import StrEnum

from avarch.application.performance_candidates import PerformanceCandidate


class BenchmarkSampleReason(StrEnum):
    SELECTED = "selected"
    TOO_SHORT = "too_short"
    NOT_SEEKABLE = "not_seekable"
    CUSTOM_PIPELINE = "custom_pipeline"
    NO_COMPATIBLE_CANDIDATES = "no_compatible_candidates"


@dataclass(frozen=True, slots=True)
class BenchmarkSamplePolicy:
    target_sample_seconds: float = 30.0
    min_sample_seconds: float = 10.0
    frames_per_parallel_unit: int = 24

    def __post_init__(self) -> None:
        if self.target_sample_seconds <= 0 or not math.isfinite(self.target_sample_seconds):
            raise ValueError("target_sample_seconds must be finite and positive")
        if self.min_sample_seconds <= 0 or not math.isfinite(self.min_sample_seconds):
            raise ValueError("min_sample_seconds must be finite and positive")
        if self.frames_per_parallel_unit <= 0:
            raise ValueError("frames_per_parallel_unit must be positive")


@dataclass(frozen=True, slots=True)
class BenchmarkSample:
    start_seconds: float
    duration_seconds: float
    estimated_frames: int
    parallel_units: int


@dataclass(frozen=True, slots=True)
class BenchmarkSampleSelection:
    sample: BenchmarkSample | None
    candidates: tuple[PerformanceCandidate, ...]
    reason: BenchmarkSampleReason


def select_representative_sample(
    *,
    duration_seconds: float | None,
    frame_rate: float | None,
    identity_seed: str,
    candidates: tuple[PerformanceCandidate, ...],
    policy: BenchmarkSamplePolicy | None = None,
    seekable: bool = True,
    custom_pipeline: bool = False,
) -> BenchmarkSampleSelection:
    policy = policy or BenchmarkSamplePolicy()
    if custom_pipeline:
        return _unavailable(BenchmarkSampleReason.CUSTOM_PIPELINE)
    if not seekable:
        return _unavailable(BenchmarkSampleReason.NOT_SEEKABLE)
    if (
        duration_seconds is None
        or frame_rate is None
        or not math.isfinite(duration_seconds)
        or not math.isfinite(frame_rate)
        or duration_seconds < policy.min_sample_seconds * 2
        or frame_rate <= 0
    ):
        return _unavailable(BenchmarkSampleReason.TOO_SHORT)

    sample_duration = min(policy.target_sample_seconds, duration_seconds / 2)
    if sample_duration < policy.min_sample_seconds:
        return _unavailable(BenchmarkSampleReason.TOO_SHORT)
    estimated_frames = max(1, math.floor(sample_duration * frame_rate))
    parallel_units = max(1, estimated_frames // policy.frames_per_parallel_unit)
    compatible_candidates = tuple(
        candidate
        for candidate in candidates
        if _candidate_parallel_units(candidate) <= parallel_units
    )
    if not compatible_candidates:
        return _unavailable(BenchmarkSampleReason.NO_COMPATIBLE_CANDIDATES)

    max_start = max(0.0, duration_seconds - sample_duration)
    start_seconds = _stable_start(identity_seed=identity_seed, max_start=max_start)
    return BenchmarkSampleSelection(
        sample=BenchmarkSample(
            start_seconds=start_seconds,
            duration_seconds=sample_duration,
            estimated_frames=estimated_frames,
            parallel_units=parallel_units,
        ),
        candidates=compatible_candidates,
        reason=BenchmarkSampleReason.SELECTED,
    )


def _candidate_parallel_units(candidate: PerformanceCandidate) -> int:
    if isinstance(candidate.workers, int):
        return candidate.workers
    return 1


def _stable_start(*, identity_seed: str, max_start: float) -> float:
    if max_start <= 0:
        return 0.0
    digest = hashlib.sha256(identity_seed.encode("utf-8")).digest()
    fraction = int.from_bytes(digest[:8], "big") / ((1 << 64) - 1)
    return round(max_start * fraction, 3)


def _unavailable(reason: BenchmarkSampleReason) -> BenchmarkSampleSelection:
    return BenchmarkSampleSelection(sample=None, candidates=(), reason=reason)
