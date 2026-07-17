from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from avarch.application.job_control import JobControlWorkflowError
from avarch.application.job_views import JobAttemptView, JobDetails
from avarch.application.scheduler_snapshot import (
    ActiveJobSummary,
    CapacitySummary,
    PipelineSummary,
    SchedulerRuntimeState,
    SchedulerRuntimeSummary,
    SchedulerSnapshot,
    WorkspaceSummary,
)
from avarch.application.scheduler_watch_controller import WatchLogPaths
from avarch.application.scheduler_watch_controls import (
    SchedulerWatchControlState,
    read_bounded_log_tail,
)
from avarch.domain.jobs import AttemptStatus, JobStage, JobStatus


def test_one_active_job_is_selected_automatically() -> None:
    state = SchedulerWatchControlState()

    state.refresh(_snapshot(active_jobs=(_active_job(42),)))

    assert state.selected_job_id == 42


def test_multiple_active_jobs_require_explicit_selection() -> None:
    state = SchedulerWatchControlState()
    snapshot = _snapshot(active_jobs=(_active_job(42), _active_job(43)))

    state.refresh(snapshot)
    assert state.selected_job_id is None
    selection = state.select_job(job_id=43, snapshot=snapshot)

    assert selection.action == "selected"
    assert state.selected_job_id == 43


def test_cancel_key_opens_confirmation_without_cancelling_immediately() -> None:
    state = SchedulerWatchControlState()
    controller = _Controller()
    action = state.handle_key("c", snapshot=_snapshot(active_jobs=(_active_job(42),)), now=_NOW)

    assert action is not None
    assert action.action == "confirm_cancel"
    assert state.cancel_confirmation is not None
    assert state.cancel_confirmation.job_id == 42
    assert controller.cancelled == []


def test_confirmation_expires_or_can_be_dismissed() -> None:
    state = SchedulerWatchControlState(confirmation_ttl_seconds=5)
    state.handle_key("c", snapshot=_snapshot(active_jobs=(_active_job(42),)), now=_NOW)

    expired = state.confirm_cancel(controller=_Controller(), now=_NOW + timedelta(seconds=6))
    state.handle_key("c", snapshot=_snapshot(active_jobs=(_active_job(42),)), now=_NOW)
    dismissed = state.dismiss_confirmation()

    assert expired.action == "confirmation_expired"
    assert dismissed.action == "dismissed"
    assert state.cancel_confirmation is None


def test_cancel_completed_job_reports_harmless_conflict() -> None:
    state = SchedulerWatchControlState()
    state.handle_key("c", snapshot=_snapshot(active_jobs=(_active_job(42),)), now=_NOW)

    action = state.confirm_cancel(
        controller=_Controller(error=JobControlWorkflowError("Job 42 cannot be canceled.")),
        now=_NOW,
    )

    assert action.action == "cancel_conflict"
    assert action.attached is True


def test_watcher_remains_attached_after_cancellation_request() -> None:
    state = SchedulerWatchControlState()
    controller = _Controller()
    state.handle_key("c", snapshot=_snapshot(active_jobs=(_active_job(42),)), now=_NOW)

    action = state.confirm_cancel(controller=controller, now=_NOW)

    assert action.action == "cancel_requested"
    assert action.attached is True
    assert controller.cancelled == [(42, "watch")]


def test_enter_toggles_selected_job_details() -> None:
    state = SchedulerWatchControlState()
    controller = _Controller(details=_job_details(42))
    state.refresh(_snapshot(active_jobs=(_active_job(42),)))

    shown = state.handle_details_key(controller=controller)
    enriched = state.apply_to_snapshot(_snapshot(active_jobs=(_active_job(42),)))
    hidden = state.handle_details_key(controller=controller)

    assert shown.action == "details_visible"
    assert enriched.watch_details is not None
    assert enriched.watch_details.job_id == 42
    assert enriched.watch_details.attempt_number == 2
    assert hidden.action == "details_hidden"
    assert state.details is None


def test_l_toggles_bounded_log_tail_panel(tmp_path: Path) -> None:
    log = tmp_path / "stderr.log"
    log.write_text("one\ntwo\nthree\n", encoding="utf-8")
    state = SchedulerWatchControlState(log_tail_lines=2)
    controller = _Controller(stderr_log=str(log))
    state.refresh(_snapshot(active_jobs=(_active_job(42),)))

    shown = state.handle_logs_key(controller=controller)
    enriched = state.apply_to_snapshot(_snapshot(active_jobs=(_active_job(42),)))
    hidden = state.handle_logs_key(controller=controller)

    assert shown.action == "logs_visible"
    assert enriched.watch_log_tail is not None
    assert enriched.watch_log_tail.lines == ("two", "three")
    assert hidden.action == "logs_hidden"
    assert state.log_tail is None


def test_log_tail_handles_missing_or_rotated_logs(tmp_path: Path) -> None:
    tail = read_bounded_log_tail(
        str(tmp_path / "missing.log"),
        max_lines=3,
        max_line_chars=80,
    )

    assert tail.missing is True
    assert tail.lines == ()


def test_log_tail_truncates_long_lines(tmp_path: Path) -> None:
    log = tmp_path / "stderr.log"
    log.write_text("short\n" + ("x" * 20) + "\n", encoding="utf-8")

    tail = read_bounded_log_tail(str(log), max_lines=5, max_line_chars=8)

    assert tail.truncated is True
    assert tail.lines == ("short", "xxxxxxxx...")


_NOW = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)


class _Controller:
    def __init__(
        self,
        *,
        error: Exception | None = None,
        details: JobDetails | None = None,
        stderr_log: str | None = None,
    ) -> None:
        self._error = error
        self._details = details
        self._stderr_log = stderr_log
        self.cancelled: list[tuple[int, str | None]] = []

    def pause(self, *, reason: str | None = None) -> object:
        del reason
        return object()

    def resume(self) -> object:
        return object()

    def cancel(self, *, job_id: int, reason: str | None = None) -> object:
        if self._error is not None:
            raise self._error
        self.cancelled.append((job_id, reason))
        return object()

    def details(self, *, job_id: int) -> JobDetails | None:
        del job_id
        return self._details

    def log_paths(self, *, job_id: int, attempt_number: int | None = None) -> WatchLogPaths:
        del job_id, attempt_number
        return WatchLogPaths(stdout_log=None, stderr_log=self._stderr_log)

    def detach(self) -> object:
        return object()


def _snapshot(*, active_jobs: tuple[ActiveJobSummary, ...]) -> SchedulerSnapshot:
    return SchedulerSnapshot(
        captured_at=_NOW,
        workspace=WorkspaceSummary(root_path="/workspace"),
        scheduler=SchedulerRuntimeSummary(state=SchedulerRuntimeState.RUNNING),
        pipeline=PipelineSummary(
            queued=0,
            active=len(active_jobs),
            completed=0,
            failed=0,
        ),
        active_jobs=active_jobs,
        capacity=CapacitySummary(cheap_workers=0, av1an_jobs=0, file_ops=0),
        upcoming_jobs=(),
        recent_events=(),
        alerts=(),
        resources=None,
        forecast=None,
    )


def _active_job(job_id: int) -> ActiveJobSummary:
    return ActiveJobSummary(
        job_id=job_id,
        source_path=f"/media/{job_id}.mkv",
        status=JobStatus.ENCODING,
        stage=JobStage.ENCODE,
        priority=0,
        workflow_steps=(),
    )


def _job_details(job_id: int) -> JobDetails:
    return JobDetails(
        id=job_id,
        source_path="/media/movie.mkv",
        profile_name="default",
        profile_hash="profile",
        queue_key="queue",
        priority=0,
        status=JobStatus.ENCODING,
        stage=JobStage.ENCODE,
        claimed_by="runner",
        attempts=2,
        created_at=_NOW,
        started_at=_NOW,
        finished_at=None,
        cancel_requested_at=None,
        hold_requested_at=None,
        probe_hash="probe",
        plan_hash="plan",
        plan_path="/plans/plan.json",
        output_path="/work/movie.av1.mkv",
        latest_validation_id=None,
        latest_promotion_id=None,
        last_error_type="ExecutionError",
        last_error_message="encoder failed",
        attempt_history=[
            JobAttemptView(
                attempt_number=2,
                stage=JobStage.ENCODE,
                status=AttemptStatus.RUNNING,
                runner_id="runner",
                stdout_log="/logs/stdout.log",
                stderr_log="/logs/stderr.log",
            )
        ],
        events=[],
    )
