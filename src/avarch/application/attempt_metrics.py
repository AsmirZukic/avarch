from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from avarch.domain.jobs import AttemptStatus
from avarch.domain.progress import ProgressPhase, ProgressSnapshot, ProgressUnit
from avarch.models.execution import ProcessResourceSummary

ATTEMPT_METRICS_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class AttemptResourceSample:
    observed_at: datetime
    rss_bytes: int | None = None
    cgroup_memory_current_bytes: int | None = None
    cpu_utilization_percent: float | None = None
    swap_current_bytes: int | None = None
    cpu_throttled_events: int | None = None
    cpu_throttled_usec: int | None = None

    def __post_init__(self) -> None:
        _validate_optional_non_negative_int("rss_bytes", self.rss_bytes)
        _validate_optional_non_negative_int(
            "cgroup_memory_current_bytes",
            self.cgroup_memory_current_bytes,
        )
        _validate_optional_non_negative_float(
            "cpu_utilization_percent",
            self.cpu_utilization_percent,
        )
        _validate_optional_non_negative_int("swap_current_bytes", self.swap_current_bytes)
        _validate_optional_non_negative_int(
            "cpu_throttled_events",
            self.cpu_throttled_events,
        )
        _validate_optional_non_negative_int("cpu_throttled_usec", self.cpu_throttled_usec)


@dataclass(frozen=True, slots=True)
class AttemptMetricsSummary:
    schema_version: int
    total_frames: int | None
    observation_duration_seconds: float | None
    aggregate_fps: float | None
    peak_rss_bytes: int | None
    peak_cgroup_memory_bytes: int | None
    average_cpu_utilization_percent: float | None
    swap_current_bytes_delta: int | None
    cpu_throttled_events_delta: int | None
    cpu_throttled_usec_delta: int | None
    memory_oom_events_delta: int | None
    memory_oom_kill_events_delta: int | None
    resource_attribution_available: bool
    incomplete: bool
    progress_samples_observed: int
    resource_samples_observed: int
    warmup_seconds: float


@dataclass(frozen=True, slots=True)
class _ProgressPoint:
    observed_at: datetime
    frames: float


class AttemptMetricsAccumulator:
    def __init__(self, *, warmup: timedelta = timedelta()) -> None:
        if warmup < timedelta():
            raise ValueError("warmup must be non-negative")
        self._warmup = warmup
        self._encoding_started_at: datetime | None = None
        self._progress_points: list[_ProgressPoint] = []
        self._total_frames: float | None = None
        self._resource_samples: list[AttemptResourceSample] = []
        self._resource_summary: ProcessResourceSummary | None = None

    def record_progress(self, snapshot: ProgressSnapshot) -> None:
        if snapshot.unit is not ProgressUnit.FRAMES or snapshot.current is None:
            return

        frames = float(snapshot.current)
        self._total_frames = _max_optional(self._total_frames, frames)
        if snapshot.phase is not ProgressPhase.ENCODING:
            return

        if self._encoding_started_at is None:
            self._encoding_started_at = snapshot.observed_at
        cutoff = self._encoding_started_at + self._warmup
        if snapshot.observed_at < cutoff:
            return
        self._progress_points.append(
            _ProgressPoint(observed_at=snapshot.observed_at, frames=frames)
        )

    def record_resource_sample(self, sample: AttemptResourceSample) -> None:
        if self._resource_samples and sample.observed_at < self._resource_samples[-1].observed_at:
            raise ValueError("resource samples must be recorded in observed_at order")
        self._resource_samples.append(sample)

    def record_resource_summary(self, summary: ProcessResourceSummary) -> None:
        self._resource_summary = summary

    def finalize(self, *, status: AttemptStatus) -> AttemptMetricsSummary:
        progress_points = tuple(self._progress_points)
        resource_samples = self._post_warmup_resource_samples()
        duration = _observation_duration(progress_points)
        return AttemptMetricsSummary(
            schema_version=ATTEMPT_METRICS_SCHEMA_VERSION,
            total_frames=_int_or_none(self._total_frames),
            observation_duration_seconds=duration,
            aggregate_fps=_aggregate_fps(progress_points, duration_seconds=duration),
            peak_rss_bytes=_max_available(
                (
                    *(sample.rss_bytes for sample in resource_samples),
                    None
                    if self._resource_summary is None
                    else self._resource_summary.peak_rss_bytes,
                )
            ),
            peak_cgroup_memory_bytes=_max_available(
                sample.cgroup_memory_current_bytes for sample in resource_samples
            ),
            average_cpu_utilization_percent=_average_available(
                sample.cpu_utilization_percent for sample in resource_samples
            ),
            swap_current_bytes_delta=_first_last_delta(
                sample.swap_current_bytes for sample in resource_samples
            )
            if self._resource_summary is None
            else self._resource_summary.swap_current_bytes_delta,
            cpu_throttled_events_delta=_first_last_delta(
                sample.cpu_throttled_events for sample in resource_samples
            )
            if self._resource_summary is None
            else self._resource_summary.cpu_throttled_events_delta,
            cpu_throttled_usec_delta=_first_last_delta(
                sample.cpu_throttled_usec for sample in resource_samples
            )
            if self._resource_summary is None
            else self._resource_summary.cpu_throttled_usec_delta,
            memory_oom_events_delta=(
                None
                if self._resource_summary is None
                else self._resource_summary.memory_oom_events_delta
            ),
            memory_oom_kill_events_delta=(
                None
                if self._resource_summary is None
                else self._resource_summary.memory_oom_kill_events_delta
            ),
            resource_attribution_available=(
                False
                if self._resource_summary is None
                else self._resource_summary.attribution_available
            ),
            incomplete=status is not AttemptStatus.COMPLETED,
            progress_samples_observed=len(progress_points),
            resource_samples_observed=len(resource_samples),
            warmup_seconds=self._warmup.total_seconds(),
        )

    def _post_warmup_resource_samples(self) -> tuple[AttemptResourceSample, ...]:
        if self._encoding_started_at is None:
            return tuple(self._resource_samples)
        cutoff = self._encoding_started_at + self._warmup
        return tuple(sample for sample in self._resource_samples if sample.observed_at >= cutoff)


def _observation_duration(points: tuple[_ProgressPoint, ...]) -> float | None:
    if len(points) < 2:
        return None
    duration = (points[-1].observed_at - points[0].observed_at).total_seconds()
    return duration if duration > 0 else None


def _aggregate_fps(
    points: tuple[_ProgressPoint, ...],
    *,
    duration_seconds: float | None,
) -> float | None:
    if duration_seconds is None or len(points) < 2:
        return None
    frames = points[-1].frames - points[0].frames
    if frames <= 0:
        return None
    return frames / duration_seconds


def _max_available(values: Iterable[int | None]) -> int | None:
    available = [value for value in values if isinstance(value, int)]
    return max(available) if available else None


def _average_available(values: Iterable[float | None]) -> float | None:
    available = [float(value) for value in values if isinstance(value, int | float)]
    if not available:
        return None
    return sum(available) / len(available)


def _first_last_delta(values: Iterable[int | None]) -> int | None:
    available = [value for value in values if isinstance(value, int)]
    if len(available) < 2:
        return None
    return max(0, available[-1] - available[0])


def _int_or_none(value: float | None) -> int | None:
    if value is None:
        return None
    return int(value)


def _max_optional(left: float | None, right: float) -> float:
    return right if left is None else max(left, right)


def _validate_optional_non_negative_int(label: str, value: int | None) -> None:
    if value is None:
        return
    if value < 0:
        raise ValueError(f"{label} must be non-negative")


def _validate_optional_non_negative_float(label: str, value: float | None) -> None:
    if value is None:
        return
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{label} must be finite and non-negative")
