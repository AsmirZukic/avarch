from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
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


def phase_progress_percent(snapshot: ProgressSnapshot) -> float | None:
    if snapshot.current is None or snapshot.total is None:
        return None
    if snapshot.total <= 0:
        return None
    percent = (snapshot.current / snapshot.total) * 100.0
    return min(100.0, max(0.0, percent))
