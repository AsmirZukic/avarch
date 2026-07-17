from __future__ import annotations

from datetime import UTC, datetime, timedelta

from avarch.application.job_views import CurrentJobProgressView, JobAttemptView
from avarch.application.progress_views import job_progress_view
from avarch.domain.jobs import AttemptStatus, JobStage, JobStatus
from avarch.domain.progress import (
    ProgressPhase,
    ProgressSnapshot,
    ProgressSource,
    ProgressUnit,
)

NOW = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)


def test_progress_view_returns_none_for_missing_job() -> None:
    assert job_progress_view(None, now=NOW) is None


def test_progress_view_for_queued_job_without_attempt() -> None:
    view = job_progress_view(
        CurrentJobProgressView(
            job_id=7,
            job_status=JobStatus.QUEUED,
            job_stage=JobStage.ENCODE,
            attempt_id=None,
            attempt=None,
            progress=None,
        ),
        now=NOW,
    )

    assert view is not None
    assert view.job_id == 7
    assert view.phase is None
    assert view.percent is None
    assert view.eta is None
    assert view.heartbeat_stale is False
    assert view.not_advancing is False


def test_progress_view_for_preparing_attempt() -> None:
    view = job_progress_view(_current(_snapshot(ProgressPhase.PREPARING, current=None)), now=NOW)

    assert view is not None
    assert view.attempt_id == 42
    assert view.attempt_number == 2
    assert view.attempt_status == AttemptStatus.RUNNING
    assert view.phase == ProgressPhase.PREPARING
    assert view.elapsed == timedelta(seconds=11)
    assert view.percent is None


def test_progress_view_for_numeric_encoding_progress() -> None:
    view = job_progress_view(
        _current(
            _snapshot(
                ProgressPhase.ENCODING,
                current=48,
                total=120,
                rate_per_second=12,
                speed_ratio=1.5,
                message="48/120 frames",
                chunks_current=2,
                chunks_total=6,
                bitrate_kbps=1500,
                estimated_output_bytes=2_000_000,
                written_output_bytes=1_000_000,
            )
        ),
        now=NOW,
    )

    assert view is not None
    assert view.percent == 40.0
    assert view.eta == timedelta(seconds=6)
    assert view.rate_per_second == 12.0
    assert view.speed_ratio == 1.5
    assert view.message == "48/120 frames"
    assert view.chunks_current == 2
    assert view.chunks_total == 6
    assert view.bitrate_kbps == 1500
    assert view.estimated_output_bytes == 2_000_000
    assert view.written_output_bytes == 1_000_000


def test_progress_view_for_unknown_total_has_no_percent_or_eta() -> None:
    view = job_progress_view(
        _current(_snapshot(ProgressPhase.ENCODING, current=48, total=None, rate_per_second=12)),
        now=NOW,
    )

    assert view is not None
    assert view.percent is None
    assert view.eta is None


def test_progress_view_for_fresh_heartbeat_without_recent_advancement() -> None:
    snapshot = _snapshot(
        ProgressPhase.ENCODING,
        current=48,
        total=120,
        heartbeat_at=NOW - timedelta(seconds=3),
        advanced_at=NOW - timedelta(minutes=2),
    )

    view = job_progress_view(_current(snapshot), now=NOW)

    assert view is not None
    assert view.heartbeat_age == timedelta(seconds=3)
    assert view.advance_age == timedelta(minutes=2)
    assert view.heartbeat_stale is False
    assert view.not_advancing is True


def test_progress_view_marks_stale_heartbeat() -> None:
    snapshot = _snapshot(
        ProgressPhase.ENCODING,
        heartbeat_at=NOW - timedelta(seconds=45),
        advanced_at=NOW - timedelta(seconds=5),
    )

    view = job_progress_view(_current(snapshot), now=NOW)

    assert view is not None
    assert view.heartbeat_stale is True
    assert view.not_advancing is False


def test_progress_view_for_muxing_phase() -> None:
    view = job_progress_view(_current(_snapshot(ProgressPhase.MUXING, current=None)), now=NOW)

    assert view is not None
    assert view.phase == ProgressPhase.MUXING
    assert view.elapsed == timedelta(seconds=11)


def test_progress_view_for_completed_phase_is_not_stale() -> None:
    snapshot = _snapshot(
        ProgressPhase.COMPLETED,
        current=120,
        total=120,
        heartbeat_at=NOW - timedelta(hours=1),
        advanced_at=NOW - timedelta(hours=1),
    )

    view = job_progress_view(
        _current(snapshot, job_status=JobStatus.PROMOTED, attempt_status=AttemptStatus.COMPLETED),
        now=NOW,
    )

    assert view is not None
    assert view.percent == 100.0
    assert view.eta is None
    assert view.heartbeat_stale is False
    assert view.not_advancing is False


def test_progress_view_for_failed_phase_is_terminal() -> None:
    view = job_progress_view(
        _current(
            _snapshot(ProgressPhase.FAILED, current=30, total=120),
            job_status=JobStatus.FAILED,
            attempt_status=AttemptStatus.FAILED,
        ),
        now=NOW,
    )

    assert view is not None
    assert view.phase == ProgressPhase.FAILED
    assert view.eta is None
    assert view.not_advancing is False


def test_progress_view_for_cancelled_phase_is_terminal() -> None:
    view = job_progress_view(
        _current(
            _snapshot(ProgressPhase.CANCELLED, current=30, total=120),
            job_status=JobStatus.CANCELLED,
            attempt_status=AttemptStatus.CANCELED,
        ),
        now=NOW,
    )

    assert view is not None
    assert view.phase == ProgressPhase.CANCELLED
    assert view.heartbeat_stale is False


def test_progress_view_for_retried_job_uses_latest_attempt_identity() -> None:
    view = job_progress_view(
        _current(
            _snapshot(ProgressPhase.ENCODING, current=5, total=120),
            attempt_id=99,
            attempt_number=3,
        ),
        now=NOW,
    )

    assert view is not None
    assert view.attempt_id == 99
    assert view.attempt_number == 3
    assert view.current == 5


def _current(
    snapshot: ProgressSnapshot | None,
    *,
    job_status: JobStatus = JobStatus.ENCODING,
    attempt_status: AttemptStatus = AttemptStatus.RUNNING,
    attempt_id: int = 42,
    attempt_number: int = 2,
) -> CurrentJobProgressView:
    return CurrentJobProgressView(
        job_id=11,
        job_status=job_status,
        job_stage=JobStage.ENCODE,
        attempt_id=attempt_id,
        attempt=JobAttemptView(
            attempt_number=attempt_number,
            stage=JobStage.ENCODE,
            status=attempt_status,
            runner_id="runner",
            stdout_log=None,
            stderr_log=None,
        ),
        progress=snapshot,
    )


def _snapshot(
    phase: ProgressPhase,
    *,
    current: float | None = 10,
    total: float | None = 100,
    rate_per_second: float | None = 10,
    speed_ratio: float | None = None,
    message: str | None = None,
    heartbeat_at: datetime | None = None,
    advanced_at: datetime | None = None,
    chunks_current: int | None = None,
    chunks_total: int | None = None,
    bitrate_kbps: int | None = None,
    estimated_output_bytes: int | None = None,
    written_output_bytes: int | None = None,
) -> ProgressSnapshot:
    observed_at = NOW - timedelta(seconds=1)
    phase_started_at = min(
        heartbeat_at or observed_at,
        advanced_at or observed_at,
        observed_at,
    ) - timedelta(seconds=10)
    return ProgressSnapshot(
        phase=phase,
        current=current,
        total=total,
        unit=ProgressUnit.FRAMES if current is not None or total is not None else None,
        rate_per_second=rate_per_second,
        speed_ratio=speed_ratio,
        source=ProgressSource.SCHEDULER,
        message=message,
        phase_started_at=phase_started_at,
        observed_at=observed_at,
        heartbeat_at=heartbeat_at or observed_at,
        advanced_at=advanced_at or observed_at,
        chunks_current=chunks_current,
        chunks_total=chunks_total,
        bitrate_kbps=bitrate_kbps,
        estimated_output_bytes=estimated_output_bytes,
        written_output_bytes=written_output_bytes,
    )
