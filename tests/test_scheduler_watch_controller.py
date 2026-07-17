from __future__ import annotations

from datetime import UTC, datetime

from avarch.application.job_views import (
    JobAttemptView,
    JobDetails,
    JobListItem,
    RecentFailedJob,
    WorkflowJobItem,
)
from avarch.application.scheduler_watch_controller import SchedulerWatchController
from avarch.domain.jobs import AttemptStatus, JobStage, JobStatus


def test_watch_controller_pauses_and_resumes_scheduler() -> None:
    scheduler = _SchedulerStore()
    controller = _controller(scheduler_store=scheduler)

    pause = controller.pause(reason="operator")
    resume = controller.resume()

    assert pause.action == "pause"
    assert resume.action == "resume"
    assert scheduler.calls == [
        ("pause", _NOW, "operator"),
        ("resume", _NOW, None),
    ]


def test_watch_controller_cancels_selected_job() -> None:
    job_store = _JobStore()
    controller = _controller(job_store=job_store)

    result = controller.cancel(job_id=42, reason="watch")

    assert result.action == "cancel"
    assert job_store.cancelled == [(42, "watch", "watcher", _NOW)]


def test_watch_controller_fetches_job_details() -> None:
    details = _job_details(42)
    job_views = _JobViewStore(details=details)
    controller = _controller(job_view_store=job_views)

    assert controller.details(job_id=42) == details


def test_watch_controller_resolves_latest_attempt_log_paths() -> None:
    job_views = _JobViewStore(
        latest=JobAttemptView(
            attempt_number=2,
            stage=JobStage.ENCODE,
            status=AttemptStatus.RUNNING,
            runner_id="runner",
            stdout_log="/logs/stdout.log",
            stderr_log="/logs/stderr.log",
        )
    )
    controller = _controller(job_view_store=job_views)

    paths = controller.log_paths(job_id=42)

    assert paths.stdout_log == "/logs/stdout.log"
    assert paths.stderr_log == "/logs/stderr.log"
    assert job_views.latest_attempt_requests == [(42, None)]


def test_watch_controller_resolves_specific_attempt_log_paths() -> None:
    job_views = _JobViewStore(latest=None)
    controller = _controller(job_view_store=job_views)

    paths = controller.log_paths(job_id=42, attempt_number=3)

    assert paths.stdout_log is None
    assert paths.stderr_log is None
    assert job_views.latest_attempt_requests == [(42, 3)]


def test_watch_controller_detach_has_no_scheduler_side_call() -> None:
    scheduler = _SchedulerStore()
    job_store = _JobStore()
    controller = _controller(scheduler_store=scheduler, job_store=job_store)

    result = controller.detach()

    assert result.action == "detach"
    assert scheduler.calls == []
    assert job_store.cancelled == []


def test_watch_controller_stops_scheduler_in_owner_mode() -> None:
    scheduler = _SchedulerStore()
    controller = _controller(scheduler_store=scheduler)

    result = controller.stop(reason="owner dashboard")

    assert result.action == "stop"
    assert scheduler.calls == [("stop", _NOW, "owner dashboard")]


_NOW = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)


def _controller(
    *,
    scheduler_store: _SchedulerStore | None = None,
    job_store: _JobStore | None = None,
    job_view_store: _JobViewStore | None = None,
) -> SchedulerWatchController:
    return SchedulerWatchController(
        scheduler_store=scheduler_store or _SchedulerStore(),
        job_store=job_store or _JobStore(),
        job_view_store=job_view_store or _JobViewStore(),
        actor="watcher",
        clock=lambda: _NOW,
    )


class _SchedulerStore:
    def __init__(self) -> None:
        self.calls: list[tuple[str, datetime, str | None]] = []

    def pause(self, *, now: datetime, reason: str | None) -> None:
        self.calls.append(("pause", now, reason))

    def resume(self, *, now: datetime) -> None:
        self.calls.append(("resume", now, None))

    def drain(self, *, now: datetime, reason: str | None) -> None:
        self.calls.append(("drain", now, reason))

    def stop(self, *, now: datetime, reason: str | None) -> None:
        self.calls.append(("stop", now, reason))


class _JobStore:
    def __init__(self) -> None:
        self.cancelled: list[tuple[int, str | None, str, datetime]] = []

    def running_job_ids(self) -> list[int]:
        return []

    def cancel_job(
        self,
        *,
        job_id: int,
        actor: str,
        now: datetime,
        reason: str | None,
    ) -> None:
        self.cancelled.append((job_id, reason, actor, now))

    def hold_job(
        self,
        *,
        job_id: int,
        actor: str,
        now: datetime,
        reason: str | None,
    ) -> None:
        del job_id, actor, now, reason

    def release_job(self, *, job_id: int, actor: str, now: datetime) -> bool:
        del job_id, actor, now
        return False

    def update_job_priority(
        self,
        *,
        job_id: int,
        priority: int,
        actor: str,
        now: datetime,
    ) -> None:
        del job_id, priority, actor, now


class _JobViewStore:
    def __init__(
        self,
        *,
        details: JobDetails | None = None,
        latest: JobAttemptView | None = None,
    ) -> None:
        self._details = details
        self._latest = latest
        self.latest_attempt_requests: list[tuple[int, int | None]] = []

    def list_jobs(
        self,
        *,
        statuses: set[JobStatus] | None,
        stages: set[JobStage] | None,
        profile: str | None,
        limit: int | None,
    ) -> list[JobListItem]:
        del statuses, stages, profile, limit
        return []

    def job_details(self, *, job_id: int) -> JobDetails | None:
        return self._details if self._details is not None and self._details.id == job_id else None

    def workflow_jobs_for_plan_hashes(
        self,
        *,
        plan_hashes: tuple[str, ...],
    ) -> list[WorkflowJobItem]:
        del plan_hashes
        return []

    def recent_failed_jobs(self, *, limit: int) -> list[RecentFailedJob]:
        del limit
        return []

    def latest_attempt(
        self,
        *,
        job_id: int,
        attempt_number: int | None,
    ) -> JobAttemptView | None:
        self.latest_attempt_requests.append((job_id, attempt_number))
        return self._latest

    def current_job_progress(self, *, job_id: int) -> object | None:
        del job_id
        return None


def _job_details(job_id: int) -> JobDetails:
    return JobDetails(
        id=job_id,
        source_path="/media/movie.mkv",
        profile_name="default",
        profile_hash="profile",
        queue_key="queue",
        priority=10,
        status=JobStatus.ENCODING,
        stage=JobStage.ENCODE,
        claimed_by="runner",
        attempts=1,
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
        last_error_type=None,
        last_error_message=None,
        attempt_history=[],
        events=[],
    )
