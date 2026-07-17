from __future__ import annotations

from datetime import UTC, datetime
from io import StringIO

from rich.console import Console

from avarch.application.scheduler_snapshot import (
    ActiveJobSummary,
    AttemptProgressSummary,
    CapacitySummary,
    LifecycleEventSummary,
    PipelineSummary,
    SchedulerRuntimeState,
    SchedulerRuntimeSummary,
    SchedulerSnapshot,
    SchedulerSessionRunSummary,
    SessionSummary,
    UpcomingJobSummary,
    WorkflowStepState,
    WorkflowStepSummary,
    WorkspaceSummary,
)
from avarch.domain.jobs import AttemptStatus, JobEventType, JobStage, JobStatus
from avarch.presentation.scheduler_dashboard import DashboardMode, render_scheduler_dashboard


def test_render_wide_scheduler_dashboard_contains_core_sections() -> None:
    output = _render(_snapshot(), width=150)

    assert "Workspace" in output
    assert "/workspace" in output
    assert "Scheduler" in output
    assert "running" in output
    assert "queued" in output
    assert "completed" in output
    assert "movie.mkv" in output
    assert "default" in output
    assert "probe" in output
    assert "encode" in output
    assert "100/200 frames" in output
    assert "12.5 fps" in output
    assert "2/5 chunks" in output
    assert "1500 Kbps" in output
    assert "est." in output
    assert "1.9 MiB" in output
    assert "written 781.2 KiB" in output
    assert "ETA 8s" in output
    assert "elapsed 1m 30s" in output
    assert "last update" in output
    assert "18s ago" in output
    assert "next.mkv" in output
    assert "Capacity" in output
    assert "encode slots" in output
    assert "Recent activity unavailable" in output
    assert "Resource telemetry unavailable" in output


def test_dashboard_respects_requested_widths() -> None:
    snapshot = _snapshot(
        active_jobs=(
            _active_job(1, "/media/movie-with-a-very-long-name-Život-日本語.mkv"),
            _active_job(2, "/media/second-active-job.mkv"),
        )
    )

    for width in (160, 120, 90, 70):
        output = _render(snapshot, width=width)

        assert "movie-with" in output
        assert "second-active-job.mkv" in output
        assert "Resource telemetry unavailable" in output
        assert "Recent activity unavailable" in output
        assert all(len(line) <= width for line in output.splitlines())


def test_narrow_dashboard_uses_compact_text_layout() -> None:
    output = _render(_snapshot(), width=70)

    assert "Avarch Scheduler" in output
    assert "Pipeline queued" in output
    assert "Capacity encode slots" in output
    assert "─" not in output
    assert "│" not in output


def test_dashboard_renders_session_and_recent_activity() -> None:
    snapshot = _snapshot(
        session=SessionSummary(
            current=SchedulerSessionRunSummary(
                session_id=1,
                owner_id="runner-1",
                workspace_id="workspace",
                pid=1234,
                host="host",
                started_at=datetime(2026, 7, 17, 11, 55, tzinfo=UTC),
                active=True,
            ),
            recent=(
                SchedulerSessionRunSummary(
                    session_id=1,
                    owner_id="runner-1",
                    workspace_id="workspace",
                    pid=1234,
                    host="host",
                    started_at=datetime(2026, 7, 17, 11, 55, tzinfo=UTC),
                    active=True,
                ),
            ),
        ),
        recent_events=(
            LifecycleEventSummary(
                event_id=1,
                job_id=42,
                attempt_id=7,
                scheduler_session_id=1,
                event_type=JobEventType.STAGE_COMPLETED,
                stage=JobStage.PROMOTE,
                actor="runner-1",
                details={"saved_bytes": 2048},
                created_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
            ),
        ),
    )

    wide = _render(snapshot, width=150)
    narrow = _render(snapshot, width=70)

    assert "Session" in wide
    assert "runner-1 on host pid 1234" in wide
    assert "Recent Activity" in wide
    assert "stage_completed" in wide
    assert "saved 2.0 KiB" in wide
    assert "Session runner-1 on host pid 1234" in narrow
    assert "job 42 promote stage_completed saved 2.0 KiB" in narrow


def test_dashboard_footer_distinguishes_observer_and_owner_modes() -> None:
    observer = _render(_snapshot(), width=120, mode=DashboardMode.OBSERVER)
    owner = _render(_snapshot(), width=120, mode=DashboardMode.OWNER)

    assert "Ctrl+C detaches; scheduler remains running" in observer
    assert "Ctrl+C stops the scheduler" in owner


def _render(
    snapshot: SchedulerSnapshot,
    *,
    width: int,
    mode: DashboardMode = DashboardMode.OBSERVER,
) -> str:
    console = Console(record=True, width=width, color_system=None, file=StringIO())
    console.print(render_scheduler_dashboard(snapshot, width=width, mode=mode))
    return console.export_text()


def _snapshot(
    *,
    active_jobs: tuple[ActiveJobSummary, ...] | None = None,
    session: SessionSummary | None = None,
    recent_events: tuple[LifecycleEventSummary, ...] = (),
) -> SchedulerSnapshot:
    captured_at = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    return SchedulerSnapshot(
        captured_at=captured_at,
        workspace=WorkspaceSummary(root_path="/workspace"),
        scheduler=SchedulerRuntimeSummary(state=SchedulerRuntimeState.RUNNING),
        pipeline=PipelineSummary(
            queued=2,
            active=1,
            completed=3,
            failed=1,
            validation_failed=0,
            size_rejected=0,
            cancelled=0,
        ),
        active_jobs=active_jobs if active_jobs is not None else (_active_job(1, "/media/movie.mkv"),),
        capacity=CapacitySummary(
            cheap_workers=2,
            cheap_active=1,
            av1an_jobs=1,
            av1an_active=1,
            file_ops=1,
            file_ops_active=0,
            av1an_workers_configured=4,
        ),
        upcoming_jobs=(
            UpcomingJobSummary(
                job_id=2,
                source_path="/media/next.mkv",
                profile_name="default",
                stage=JobStage.PROBE,
                status=JobStatus.QUEUED,
                priority=5,
                selection_position=1,
                selection_confidence="current_snapshot",
            ),
        ),
        session=session,
        recent_events=recent_events,
        resources=None,
        forecast=None,
    )


def _active_job(job_id: int, source_path: str) -> ActiveJobSummary:
    return ActiveJobSummary(
        job_id=job_id,
        source_path=source_path,
        profile_name="default",
        status=JobStatus.ENCODING,
        stage=JobStage.ENCODE,
        priority=10,
        attempt=AttemptProgressSummary(
            attempt_id=job_id * 10,
            attempt_number=1,
            status=AttemptStatus.RUNNING,
            frames_current=100,
            frames_total=200,
            rate_per_second=12.5,
            speed_ratio=1.2,
            chunks_current=2,
            chunks_total=5,
            bitrate_kbps=1500,
            estimated_output_bytes=2_000_000,
            written_output_bytes=800_000,
            eta_seconds=8,
            elapsed_seconds=90,
            stale=True,
            last_update_age_seconds=18,
        ),
        workflow_steps=(
            WorkflowStepSummary(stage=JobStage.PROBE, state=WorkflowStepState.COMPLETE),
            WorkflowStepSummary(stage=JobStage.PLAN, state=WorkflowStepState.COMPLETE),
            WorkflowStepSummary(stage=JobStage.ENCODE, state=WorkflowStepState.ACTIVE),
            WorkflowStepSummary(stage=JobStage.VALIDATE, state=WorkflowStepState.PENDING),
            WorkflowStepSummary(stage=JobStage.PROMOTE, state=WorkflowStepState.PENDING),
            WorkflowStepSummary(stage=JobStage.CLEANUP, state=WorkflowStepState.SKIPPED),
        ),
    )
