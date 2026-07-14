from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from avarch.application.job_views import CurrentJobProgressView
from avarch.domain.jobs import AttemptStatus, JobStage, JobStatus
from avarch.domain.progress import (
    TERMINAL_PROGRESS_PHASES,
    ProgressPhase,
    ProgressSnapshot,
    ProgressSource,
    ProgressUnit,
    derive_progress_timing,
    phase_progress_percent,
)

DEFAULT_HEARTBEAT_STALE_AFTER = timedelta(seconds=30)
DEFAULT_ADVANCEMENT_STALE_AFTER = timedelta(seconds=60)


@dataclass(frozen=True, slots=True)
class JobProgressView:
    job_id: int
    job_status: JobStatus
    job_stage: JobStage
    attempt_id: int | None
    attempt_number: int | None
    attempt_status: AttemptStatus | None
    phase: ProgressPhase | None
    current: int | float | None
    total: int | float | None
    unit: ProgressUnit | None
    percent: float | None
    elapsed: timedelta | None
    eta: timedelta | None
    rate_per_second: float | None
    speed_ratio: float | None
    source: ProgressSource | None
    heartbeat_age: timedelta | None
    advance_age: timedelta | None
    heartbeat_stale: bool
    not_advancing: bool
    message: str | None
    observed_at: datetime | None
    heartbeat_at: datetime | None
    advanced_at: datetime | None


def job_progress_view(
    current: CurrentJobProgressView | None,
    *,
    now: datetime,
    heartbeat_stale_after: timedelta = DEFAULT_HEARTBEAT_STALE_AFTER,
    advancement_stale_after: timedelta = DEFAULT_ADVANCEMENT_STALE_AFTER,
) -> JobProgressView | None:
    if current is None:
        return None

    attempt = current.attempt
    attempt_number = attempt.attempt_number if attempt is not None else None
    attempt_status = attempt.status if attempt is not None else None
    snapshot = current.progress
    if snapshot is None:
        return JobProgressView(
            job_id=current.job_id,
            job_status=current.job_status,
            job_stage=current.job_stage,
            attempt_id=current.attempt_id,
            attempt_number=attempt_number,
            attempt_status=attempt_status,
            phase=None,
            current=None,
            total=None,
            unit=None,
            percent=None,
            elapsed=None,
            eta=None,
            rate_per_second=None,
            speed_ratio=None,
            source=None,
            heartbeat_age=None,
            advance_age=None,
            heartbeat_stale=False,
            not_advancing=False,
            message=None,
            observed_at=None,
            heartbeat_at=None,
            advanced_at=None,
        )

    timing_now = _compatible_now(snapshot.observed_at, now)
    timing = derive_progress_timing(
        snapshot,
        now=timing_now,
        heartbeat_stale_after=heartbeat_stale_after,
        advancement_stale_after=advancement_stale_after,
    )
    return JobProgressView(
        job_id=current.job_id,
        job_status=current.job_status,
        job_stage=current.job_stage,
        attempt_id=current.attempt_id,
        attempt_number=attempt_number,
        attempt_status=attempt_status,
        phase=snapshot.phase,
        current=snapshot.current,
        total=snapshot.total,
        unit=snapshot.unit,
        percent=phase_progress_percent(snapshot),
        elapsed=timing.phase_elapsed,
        eta=_eta_from_snapshot(snapshot),
        rate_per_second=snapshot.rate_per_second,
        speed_ratio=snapshot.speed_ratio,
        source=snapshot.source,
        heartbeat_age=timing.heartbeat_age,
        advance_age=timing.advance_age,
        heartbeat_stale=timing.heartbeat_stale,
        not_advancing=timing.not_advancing,
        message=snapshot.message,
        observed_at=snapshot.observed_at,
        heartbeat_at=snapshot.heartbeat_at,
        advanced_at=snapshot.advanced_at,
    )


def _eta_from_snapshot(snapshot: ProgressSnapshot) -> timedelta | None:
    if snapshot.phase in TERMINAL_PROGRESS_PHASES:
        return None
    current = snapshot.current
    total = snapshot.total
    rate = snapshot.rate_per_second
    if current is None or total is None or rate is None:
        return None
    if rate <= 0 or not math.isfinite(rate):
        return None
    remaining = total - current
    if remaining <= 0:
        return None
    return timedelta(seconds=remaining / rate)


def _compatible_now(reference: datetime, now: datetime) -> datetime:
    if reference.tzinfo is None and now.tzinfo is not None:
        return now.replace(tzinfo=None)
    if reference.tzinfo is not None and now.tzinfo is None:
        return now.replace(tzinfo=reference.tzinfo)
    return now
