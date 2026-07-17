from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from avarch.application.scheduler_snapshot import (
    ActiveJobSummary,
    AttemptProgressSummary,
    CapacitySummary,
    PipelineSummary,
    SchedulerRuntimeState,
    SchedulerRuntimeSummary,
    SchedulerSnapshot,
    WorkflowStepState,
    WorkflowStepSummary,
    WorkspaceSummary,
)
from avarch.domain.jobs import AttemptStatus, JobStage, JobStatus


def test_snapshot_models_reject_negative_counts() -> None:
    with pytest.raises(ValidationError):
        PipelineSummary(queued=-1, active=0, completed=0, failed=0)

    with pytest.raises(ValidationError):
        CapacitySummary(cheap_workers=1, cheap_active=-1, av1an_jobs=1, file_ops=1)


def test_byte_and_frame_values_are_strict_integers_not_formatted_strings() -> None:
    with pytest.raises(ValidationError):
        AttemptProgressSummary(
            attempt_id=1,
            attempt_number=1,
            status=AttemptStatus.RUNNING,
            frames_current="1,024",
        )

    with pytest.raises(ValidationError):
        AttemptProgressSummary(
            attempt_id=1,
            attempt_number=1,
            status=AttemptStatus.RUNNING,
            eta_seconds="11.8/15.6G",
        )


def test_optional_metrics_remain_none_when_unavailable() -> None:
    progress = AttemptProgressSummary(
        attempt_id=1,
        attempt_number=1,
        status=AttemptStatus.RUNNING,
    )

    assert progress.frames_current is None
    assert progress.frames_total is None
    assert progress.fps is None
    assert progress.eta_seconds is None


def test_snapshot_supports_multiple_active_jobs() -> None:
    snapshot = _snapshot(
        active_jobs=(
            _active_job(1, "first.mkv"),
            _active_job(2, "second.mkv"),
        )
    )

    assert [job.job_id for job in snapshot.active_jobs] == [1, 2]


def test_snapshot_serialization_is_deterministic_and_uses_raw_values() -> None:
    snapshot = _snapshot(active_jobs=(_active_job(1, "movie.mkv"),))

    first = snapshot.to_canonical_json()
    second = snapshot.to_canonical_json()

    assert first == second
    assert '"captured_at":"2026-07-17T12:00:00Z"' in first
    assert '"frames_current":100' in first
    assert "100/200" not in first
    assert "rich" not in first.lower()


def test_every_snapshot_includes_one_captured_at_value() -> None:
    snapshot = _snapshot(active_jobs=())

    assert snapshot.captured_at == datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    assert snapshot.to_canonical_json().count("captured_at") == 1


def _snapshot(*, active_jobs: tuple[ActiveJobSummary, ...]) -> SchedulerSnapshot:
    return SchedulerSnapshot(
        captured_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        workspace=WorkspaceSummary(root_path="/workspace", database_url="sqlite:///workspace/db.sqlite"),
        scheduler=SchedulerRuntimeSummary(state=SchedulerRuntimeState.RUNNING),
        pipeline=PipelineSummary(queued=3, active=len(active_jobs), completed=5, failed=1),
        active_jobs=active_jobs,
        capacity=CapacitySummary(cheap_workers=2, av1an_jobs=1, file_ops=1),
    )


def _active_job(job_id: int, source_path: str) -> ActiveJobSummary:
    return ActiveJobSummary(
        job_id=job_id,
        source_path=source_path,
        profile_name="default",
        status=JobStatus.ENCODING,
        stage=JobStage.ENCODE,
        priority=0,
        attempt=AttemptProgressSummary(
            attempt_id=job_id * 10,
            attempt_number=1,
            status=AttemptStatus.RUNNING,
            frames_current=100,
            frames_total=200,
        ),
        workflow_steps=(
            WorkflowStepSummary(stage=JobStage.PROBE, state=WorkflowStepState.COMPLETE),
            WorkflowStepSummary(stage=JobStage.PLAN, state=WorkflowStepState.COMPLETE),
            WorkflowStepSummary(stage=JobStage.ENCODE, state=WorkflowStepState.ACTIVE),
        ),
    )
