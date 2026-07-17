from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


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
