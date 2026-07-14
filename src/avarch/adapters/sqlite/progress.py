from __future__ import annotations

from datetime import UTC, datetime

from sqlmodel import Session

from avarch.adapters.sqlite.models import JobAttempt, JobAttemptProgress
from avarch.domain.progress import (
    TERMINAL_PROGRESS_PHASES,
    ProgressPhase,
    ProgressSnapshot,
    ProgressSource,
    ProgressUnit,
)

__all__ = [
    "ProgressPersistenceError",
    "SqliteProgressStore",
]


class ProgressPersistenceError(RuntimeError):
    pass


class SqliteProgressStore:
    def __init__(self, session: Session) -> None:
        self._session = session

    def save_snapshot(
        self,
        *,
        attempt_id: int,
        snapshot: ProgressSnapshot,
        persisted_at: datetime,
    ) -> bool:
        if self._session.get(JobAttempt, attempt_id) is None:
            raise ProgressPersistenceError(f"Attempt not found: {attempt_id}")

        observed_at = _sqlite_datetime(snapshot.observed_at)
        existing = self._session.get(JobAttemptProgress, attempt_id)
        if existing is None:
            row = _new_progress_row(
                attempt_id=attempt_id,
                snapshot=snapshot,
                persisted_at=persisted_at,
            )
            self._session.add(row)
            return True

        if _should_ignore(existing, snapshot=snapshot, observed_at=observed_at):
            return False

        _apply_snapshot(existing, snapshot=snapshot)
        existing.updated_at = _sqlite_datetime(persisted_at)
        self._session.add(existing)
        return True

    def get_snapshot(self, *, attempt_id: int) -> ProgressSnapshot | None:
        row = self._session.get(JobAttemptProgress, attempt_id)
        if row is None:
            return None
        return ProgressSnapshot(
            phase=ProgressPhase(row.phase),
            current=row.current_value,
            total=row.total_value,
            unit=ProgressUnit(row.unit) if row.unit is not None else None,
            rate_per_second=row.rate_per_second,
            speed_ratio=row.speed_ratio,
            source=ProgressSource(row.source),
            message=row.message,
            phase_started_at=row.phase_started_at,
            observed_at=row.observed_at,
            heartbeat_at=row.heartbeat_at,
            advanced_at=row.advanced_at,
        )

    def finalize_snapshot(
        self,
        *,
        attempt_id: int,
        snapshot: ProgressSnapshot,
        persisted_at: datetime,
    ) -> bool:
        if snapshot.phase not in TERMINAL_PROGRESS_PHASES:
            raise ProgressPersistenceError(
                f"Cannot finalize non-terminal progress phase: {snapshot.phase}"
            )
        return self.save_snapshot(
            attempt_id=attempt_id,
            snapshot=snapshot,
            persisted_at=persisted_at,
        )


def _new_progress_row(
    *,
    attempt_id: int,
    snapshot: ProgressSnapshot,
    persisted_at: datetime,
) -> JobAttemptProgress:
    row = JobAttemptProgress(
        attempt_id=attempt_id,
        phase=snapshot.phase,
        current_value=None,
        total_value=None,
        unit=None,
        rate_per_second=None,
        speed_ratio=None,
        source=snapshot.source,
        message=snapshot.message,
        phase_started_at=_sqlite_datetime(snapshot.phase_started_at),
        observed_at=_sqlite_datetime(snapshot.observed_at),
        heartbeat_at=_sqlite_datetime(snapshot.heartbeat_at),
        advanced_at=_sqlite_datetime(snapshot.advanced_at) if snapshot.advanced_at else None,
        created_at=_sqlite_datetime(persisted_at),
        updated_at=_sqlite_datetime(persisted_at),
    )
    _apply_numeric_fields(row, snapshot=snapshot)
    return row


def _apply_snapshot(row: JobAttemptProgress, *, snapshot: ProgressSnapshot) -> None:
    row.phase = snapshot.phase
    _apply_numeric_fields(row, snapshot=snapshot)
    row.source = snapshot.source
    row.message = snapshot.message
    row.phase_started_at = _sqlite_datetime(snapshot.phase_started_at)
    row.observed_at = _sqlite_datetime(snapshot.observed_at)
    row.heartbeat_at = _sqlite_datetime(snapshot.heartbeat_at)
    row.advanced_at = _sqlite_datetime(snapshot.advanced_at) if snapshot.advanced_at else None


def _apply_numeric_fields(row: JobAttemptProgress, *, snapshot: ProgressSnapshot) -> None:
    row.current_value = _float_or_none(snapshot.current)
    row.total_value = _float_or_none(snapshot.total)
    row.unit = snapshot.unit
    row.rate_per_second = snapshot.rate_per_second
    row.speed_ratio = snapshot.speed_ratio


def _should_ignore(
    existing: JobAttemptProgress,
    *,
    snapshot: ProgressSnapshot,
    observed_at: datetime,
) -> bool:
    if existing.observed_at > observed_at:
        return True
    if ProgressPhase(existing.phase) != snapshot.phase:
        return False
    if existing.current_value is None or snapshot.current is None:
        return False
    return float(snapshot.current) < existing.current_value


def _float_or_none(value: int | float | None) -> float | None:
    return None if value is None else float(value)


def _sqlite_datetime(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)
