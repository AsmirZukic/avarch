from __future__ import annotations

from datetime import UTC, datetime

from rich.console import Console

from avarch.application.scheduler_snapshot import (
    ActiveJobSummary,
    AttemptProgressSummary,
    CapacitySummary,
    PipelineSummary,
    SchedulerRuntimeState,
    SchedulerRuntimeSummary,
    SchedulerSnapshot,
    UpcomingJobSummary,
    WorkflowStepState,
    WorkflowStepSummary,
    WorkspaceSummary,
)
from avarch.domain.jobs import AttemptStatus, JobStage, JobStatus
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
    assert "ETA 8s" in output
    assert "elapsed 1m 30s" in output
    assert "next.mkv" in output
    assert "Capacity" in output
    assert "encode slots" in output
    assert "Recent activity unavailable" in output
    assert "Resource telemetry unavailable" in output


def _render(snapshot: SchedulerSnapshot, *, width: int) -> str:
    console = Console(record=True, width=width, color_system=None)
    console.print(
        render_scheduler_dashboard(snapshot, width=width, mode=DashboardMode.OBSERVER)
    )
    return console.export_text()


def _snapshot() -> SchedulerSnapshot:
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
        active_jobs=(
            ActiveJobSummary(
                job_id=1,
                source_path="/media/movie.mkv",
                profile_name="default",
                status=JobStatus.ENCODING,
                stage=JobStage.ENCODE,
                priority=10,
                attempt=AttemptProgressSummary(
                    attempt_id=10,
                    attempt_number=1,
                    status=AttemptStatus.RUNNING,
                    frames_current=100,
                    frames_total=200,
                    rate_per_second=12.5,
                    speed_ratio=1.2,
                    eta_seconds=8,
                    elapsed_seconds=90,
                ),
                workflow_steps=(
                    WorkflowStepSummary(stage=JobStage.PROBE, state=WorkflowStepState.COMPLETE),
                    WorkflowStepSummary(stage=JobStage.PLAN, state=WorkflowStepState.COMPLETE),
                    WorkflowStepSummary(stage=JobStage.ENCODE, state=WorkflowStepState.ACTIVE),
                    WorkflowStepSummary(stage=JobStage.VALIDATE, state=WorkflowStepState.PENDING),
                    WorkflowStepSummary(stage=JobStage.PROMOTE, state=WorkflowStepState.PENDING),
                    WorkflowStepSummary(stage=JobStage.CLEANUP, state=WorkflowStepState.SKIPPED),
                ),
            ),
        ),
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
        recent_events=(),
        resources=None,
        forecast=None,
    )
