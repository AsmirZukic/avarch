from __future__ import annotations

from datetime import UTC, datetime, timedelta

from avarch.application.job_control import JobControlWorkflowError
from avarch.application.scheduler_snapshot import (
    ActiveJobSummary,
    CapacitySummary,
    PipelineSummary,
    SchedulerRuntimeState,
    SchedulerRuntimeSummary,
    SchedulerSnapshot,
    WorkspaceSummary,
)
from avarch.application.scheduler_watch_controls import SchedulerWatchControlState
from avarch.domain.jobs import JobStage, JobStatus


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


_NOW = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)


class _Controller:
    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error
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

    def details(self, *, job_id: int) -> None:
        del job_id
        return None

    def log_paths(self, *, job_id: int, attempt_number: int | None = None) -> object:
        del job_id, attempt_number
        return object()

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
