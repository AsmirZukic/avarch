from __future__ import annotations

from datetime import timedelta

from avarch.application.progress_views import JobProgressView
from avarch.cli import _scheduler_live_progress_enabled  # pyright: ignore[reportPrivateUsage]
from avarch.cli_rendering import (
    job_progress_compact_label,
    job_progress_eta_label,
    job_progress_phase_label,
    job_progress_updated_label,
    plain_job_progress_line,
    select_progress_watch_render_mode,
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


def test_watch_render_mode_selects_live_for_tty() -> None:
    mode = select_progress_watch_render_mode(stdout_is_tty=True, no_color=None)

    assert mode.live is True
    assert mode.color is True


def test_watch_render_mode_selects_plain_for_non_tty() -> None:
    mode = select_progress_watch_render_mode(stdout_is_tty=False, no_color=None)

    assert mode.live is False
    assert mode.color is False


def test_watch_render_mode_disables_color_when_requested() -> None:
    mode = select_progress_watch_render_mode(stdout_is_tty=True, no_color="1")

    assert mode.live is True
    assert mode.color is False


def test_scheduler_live_progress_is_default_only_for_foreground_tty() -> None:
    assert _scheduler_live_progress_enabled(mode="foreground", stdout_is_tty=True) is True
    assert _scheduler_live_progress_enabled(mode="foreground", stdout_is_tty=False) is False
    assert _scheduler_live_progress_enabled(mode="detached", stdout_is_tty=True) is False


def test_plain_watch_line_includes_progress_without_ansi() -> None:
    line = plain_job_progress_line(_view(), label="episode-01.mkv")

    assert line == "episode-01.mkv encoding 40.0% elapsed=10s eta=6s speed=1.50x"
    assert "\x1b" not in line


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
