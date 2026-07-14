from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


class ProgressPhase(StrEnum):
    PREPARING = "preparing"
    SCENE_DETECTION = "scene_detection"
    ENCODING = "encoding"
    CONCATENATING = "concatenating"
    MUXING = "muxing"
    VALIDATING = "validating"
    PROMOTING = "promoting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProgressUnit(StrEnum):
    FRAMES = "frames"
    CHUNKS = "chunks"
    SECONDS = "seconds"
    BYTES = "bytes"
    UNKNOWN = "unknown"


class ProgressSource(StrEnum):
    AV1AN_STRUCTURED = "av1an_structured"
    AV1AN_STATE = "av1an_state"
    AV1AN_OUTPUT = "av1an_output"
    FFMPEG_PROGRESS = "ffmpeg_progress"
    SCHEDULER = "scheduler"
    PROCESS_HEARTBEAT = "process_heartbeat"


TERMINAL_PROGRESS_PHASES = frozenset(
    {
        ProgressPhase.COMPLETED,
        ProgressPhase.FAILED,
        ProgressPhase.CANCELLED,
    }
)


@dataclass(frozen=True, slots=True)
class ProgressSnapshot:
    phase: ProgressPhase
    current: int | float | None
    total: int | float | None
    unit: ProgressUnit | None
    rate_per_second: float | None
    speed_ratio: float | None
    source: ProgressSource
    message: str | None
    phase_started_at: datetime
    observed_at: datetime
    heartbeat_at: datetime
    advanced_at: datetime | None

    def __post_init__(self) -> None:
        _validate_non_negative("current", self.current)
        _validate_positive("total", self.total)
        _validate_optional_positive_float("rate_per_second", self.rate_per_second)
        _validate_optional_positive_float("speed_ratio", self.speed_ratio)
        if (self.current is not None or self.total is not None) and self.unit is None:
            raise ValueError("numeric progress requires a unit")
        if self.observed_at < self.phase_started_at:
            raise ValueError("observed_at must not be before phase_started_at")
        if self.heartbeat_at < self.phase_started_at:
            raise ValueError("heartbeat_at must not be before phase_started_at")
        if self.heartbeat_at > self.observed_at:
            raise ValueError("heartbeat_at must not be after observed_at")
        if self.advanced_at is not None:
            if self.advanced_at < self.phase_started_at:
                raise ValueError("advanced_at must not be before phase_started_at")
            if self.advanced_at > self.observed_at:
                raise ValueError("advanced_at must not be after observed_at")


def _validate_non_negative(label: str, value: int | float | None) -> None:
    if value is None:
        return
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{label} must be finite and non-negative")


def _validate_positive(label: str, value: int | float | None) -> None:
    if value is None:
        return
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label} must be finite and positive")


def _validate_optional_positive_float(label: str, value: float | None) -> None:
    if value is None:
        return
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{label} must be finite and non-negative")


@dataclass(frozen=True, slots=True)
class ProgressTiming:
    phase_elapsed: timedelta
    heartbeat_age: timedelta
    advance_age: timedelta | None
    heartbeat_stale: bool
    not_advancing: bool


@dataclass(frozen=True, slots=True)
class ProgressRateState:
    attempt_id: int
    phase: ProgressPhase
    current: int | float | None
    observed_at: datetime
    smoothed_rate_per_second: float | None


@dataclass(frozen=True, slots=True)
class ProgressRateEstimate:
    rate_per_second: float | None
    eta: timedelta | None


def phase_progress_percent(snapshot: ProgressSnapshot) -> float | None:
    if snapshot.current is None or snapshot.total is None:
        return None
    if snapshot.total <= 0:
        return None
    percent = (snapshot.current / snapshot.total) * 100.0
    return min(100.0, max(0.0, percent))


def derive_progress_timing(
    snapshot: ProgressSnapshot,
    *,
    now: datetime,
    heartbeat_stale_after: timedelta,
    advancement_stale_after: timedelta,
) -> ProgressTiming:
    terminal = snapshot.phase in TERMINAL_PROGRESS_PHASES
    heartbeat_age = _non_negative_duration(now - snapshot.heartbeat_at)
    advance_age = (
        _non_negative_duration(now - snapshot.advanced_at)
        if snapshot.advanced_at is not None
        else None
    )
    return ProgressTiming(
        phase_elapsed=_non_negative_duration(now - snapshot.phase_started_at),
        heartbeat_age=heartbeat_age,
        advance_age=advance_age,
        heartbeat_stale=False if terminal else heartbeat_age > heartbeat_stale_after,
        not_advancing=False
        if terminal or advance_age is None
        else advance_age > advancement_stale_after,
    )


def _non_negative_duration(duration: timedelta) -> timedelta:
    if duration < timedelta():
        return timedelta()
    return duration


def estimate_rate_and_eta(
    *,
    previous: ProgressRateState | None,
    attempt_id: int,
    snapshot: ProgressSnapshot,
    alpha: float = 0.3,
) -> tuple[ProgressRateState | None, ProgressRateEstimate]:
    if alpha <= 0 or alpha > 1 or not math.isfinite(alpha):
        raise ValueError("alpha must be finite and within (0, 1]")
    state = ProgressRateState(
        attempt_id=attempt_id,
        phase=snapshot.phase,
        current=snapshot.current,
        observed_at=snapshot.observed_at,
        smoothed_rate_per_second=None,
    )
    empty = ProgressRateEstimate(rate_per_second=None, eta=None)
    if snapshot.current is None:
        return state, empty
    if snapshot.phase in TERMINAL_PROGRESS_PHASES:
        return state, empty
    if (
        previous is None
        or previous.attempt_id != attempt_id
        or previous.phase != snapshot.phase
        or previous.current is None
    ):
        return state, empty

    elapsed = (snapshot.observed_at - previous.observed_at).total_seconds()
    advanced = snapshot.current - previous.current
    if elapsed <= 0 or advanced <= 0:
        return state, empty

    instant_rate = advanced / elapsed
    smoothed_rate = (
        instant_rate
        if previous.smoothed_rate_per_second is None
        else (alpha * instant_rate) + ((1.0 - alpha) * previous.smoothed_rate_per_second)
    )
    state = ProgressRateState(
        attempt_id=attempt_id,
        phase=snapshot.phase,
        current=snapshot.current,
        observed_at=snapshot.observed_at,
        smoothed_rate_per_second=smoothed_rate,
    )
    eta = _estimate_eta(snapshot=snapshot, rate_per_second=smoothed_rate)
    return state, ProgressRateEstimate(rate_per_second=smoothed_rate, eta=eta)


def _estimate_eta(
    *,
    snapshot: ProgressSnapshot,
    rate_per_second: float,
) -> timedelta | None:
    if snapshot.current is None or snapshot.total is None:
        return None
    if rate_per_second <= 0 or not math.isfinite(rate_per_second):
        return None
    remaining = snapshot.total - snapshot.current
    if remaining <= 0:
        return None
    return timedelta(seconds=remaining / rate_per_second)
