from __future__ import annotations

from dataclasses import replace
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
            chunks_current=row.chunks_current,
            chunks_total=row.chunks_total,
            bitrate_kbps=row.bitrate_kbps,
            estimated_output_bytes=row.estimated_output_bytes,
            written_output_bytes=row.written_output_bytes,
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
        snapshot = self._terminal_snapshot_with_last_numeric_values(
            attempt_id=attempt_id,
            snapshot=snapshot,
        )
        return self.save_snapshot(
            attempt_id=attempt_id,
            snapshot=snapshot,
            persisted_at=persisted_at,
        )

    def _terminal_snapshot_with_last_numeric_values(
        self,
        *,
        attempt_id: int,
        snapshot: ProgressSnapshot,
    ) -> ProgressSnapshot:
        if snapshot.current is not None or snapshot.total is not None:
            return snapshot
        existing = self._session.get(JobAttemptProgress, attempt_id)
        if existing is None or existing.current_value is None:
            return snapshot
        return replace(
            snapshot,
            current=existing.current_value,
            total=existing.total_value,
            unit=ProgressUnit(existing.unit) if existing.unit is not None else None,
            rate_per_second=existing.rate_per_second,
            speed_ratio=existing.speed_ratio,
            chunks_current=existing.chunks_current,
            chunks_total=existing.chunks_total,
            bitrate_kbps=existing.bitrate_kbps,
            estimated_output_bytes=existing.estimated_output_bytes,
            written_output_bytes=existing.written_output_bytes,
            advanced_at=_terminal_advanced_at(existing.advanced_at, snapshot=snapshot),
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
    preserve_existing_progress = _preserve_existing_progress(row, snapshot=snapshot)
    row.phase = snapshot.phase
    if not preserve_existing_progress:
        _apply_numeric_fields(row, snapshot=snapshot)
    row.source = snapshot.source
    row.message = snapshot.message
    if not preserve_existing_progress:
        row.phase_started_at = _sqlite_datetime(snapshot.phase_started_at)
    row.observed_at = _sqlite_datetime(snapshot.observed_at)
    row.heartbeat_at = _sqlite_datetime(snapshot.heartbeat_at)
    if not preserve_existing_progress:
        row.advanced_at = _sqlite_datetime(snapshot.advanced_at) if snapshot.advanced_at else None


def _apply_numeric_fields(row: JobAttemptProgress, *, snapshot: ProgressSnapshot) -> None:
    row.current_value = _float_or_none(snapshot.current)
    row.total_value = _float_or_none(snapshot.total)
    row.unit = snapshot.unit
    row.rate_per_second = snapshot.rate_per_second
    row.speed_ratio = snapshot.speed_ratio
    row.chunks_current = snapshot.chunks_current
    row.chunks_total = snapshot.chunks_total
    row.bitrate_kbps = snapshot.bitrate_kbps
    row.estimated_output_bytes = snapshot.estimated_output_bytes
    row.written_output_bytes = snapshot.written_output_bytes


def _preserve_existing_progress(row: JobAttemptProgress, *, snapshot: ProgressSnapshot) -> bool:
    return (
        ProgressPhase(row.phase) == snapshot.phase
        and row.current_value is not None
        and snapshot.current is None
    )


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


def _snapshot_datetime(value: datetime | None, *, reference: datetime) -> datetime | None:
    if value is None or value.tzinfo is not None or reference.tzinfo is None:
        return value
    return value.replace(tzinfo=reference.tzinfo)


def _terminal_advanced_at(value: datetime | None, *, snapshot: ProgressSnapshot) -> datetime:
    advanced_at = _snapshot_datetime(value, reference=snapshot.observed_at)
    if advanced_at is None or advanced_at < snapshot.phase_started_at:
        return snapshot.observed_at
    return advanced_at
