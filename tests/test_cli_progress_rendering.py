from __future__ import annotations

from datetime import timedelta

from avarch.application.progress_views import JobProgressView
from avarch.cli_rendering import (
    job_progress_compact_label,
    job_progress_eta_label,
    job_progress_phase_label,
    job_progress_updated_label,
)
from avarch.domain.jobs import AttemptStatus, JobStage, JobStatus
from avarch.domain.progress import ProgressPhase, ProgressSource, ProgressUnit


def test_progress_list_labels_for_missing_progress() -> None:
    assert job_progress_phase_label(None) == "—"
    assert job_progress_compact_label(None) == "—"
    assert job_progress_eta_label(None) == "—"
    assert job_progress_updated_label(None) == "—"


def test_progress_list_labels_for_phase_only_progress() -> None:
    view = _view(
        phase=ProgressPhase.PREPARING,
        current=None,
        total=None,
        unit=None,
        percent=None,
        eta=None,
    )

    assert job_progress_phase_label(view) == "preparing"
    assert job_progress_compact_label(view) == "—"
    assert job_progress_eta_label(view) == "—"


def test_progress_list_labels_for_percentage_and_eta() -> None:
    view = _view(percent=40.0, eta=timedelta(seconds=366))

    assert job_progress_compact_label(view) == "40.0%"
    assert job_progress_eta_label(view) == "6m 06s"
    assert job_progress_updated_label(view) == "3s ago"


def test_progress_list_labels_for_unknown_total() -> None:
    view = _view(total=None, percent=None, eta=None)

    assert job_progress_compact_label(view) == "48 frames"
    assert job_progress_eta_label(view) == "—"


def test_progress_list_labels_for_stale_heartbeat() -> None:
    view = _view(heartbeat_stale=True, heartbeat_age=timedelta(seconds=45))

    assert job_progress_updated_label(view) == "stale"


def _view(
    *,
    phase: ProgressPhase = ProgressPhase.ENCODING,
    current: float | None = 48,
    total: float | None = 120,
    unit: ProgressUnit | None = ProgressUnit.FRAMES,
    percent: float | None = 40,
    eta: timedelta | None = timedelta(seconds=6),
    heartbeat_stale: bool = False,
    heartbeat_age: timedelta | None = timedelta(seconds=3),
) -> JobProgressView:
    return JobProgressView(
        job_id=1,
        job_status=JobStatus.ENCODING,
        job_stage=JobStage.ENCODE,
        attempt_id=2,
        attempt_number=1,
        attempt_status=AttemptStatus.RUNNING,
        phase=phase,
        current=current,
        total=total,
        unit=unit,
        percent=percent,
        elapsed=timedelta(seconds=10),
        eta=eta,
        rate_per_second=12.0,
        speed_ratio=1.5,
        source=ProgressSource.AV1AN_OUTPUT,
        heartbeat_age=heartbeat_age,
        advance_age=timedelta(seconds=3),
        heartbeat_stale=heartbeat_stale,
        not_advancing=False,
        message=None,
        observed_at=None,
        heartbeat_at=None,
        advanced_at=None,
    )
