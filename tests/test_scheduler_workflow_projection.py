# ruff: noqa: E501

from __future__ import annotations

import pytest

from avarch.application.scheduler_snapshot import (
    WorkflowStepState,
    project_workflow_steps,
)
from avarch.domain.jobs import AttemptStatus, JobStage, JobStatus


@pytest.mark.parametrize(
    ("label", "job_status", "job_stage", "attempt_status", "expected"),
    [
        (
            "queued probe",
            JobStatus.QUEUED,
            JobStage.PROBE,
            None,
            "probe:pending plan:pending scene_detect:pending encode:pending validate:pending promote:pending cleanup:skipped",
        ),
        (
            "active probe",
            JobStatus.ENCODING,
            JobStage.PROBE,
            AttemptStatus.RUNNING,
            "probe:active plan:pending scene_detect:pending encode:pending validate:pending promote:pending cleanup:skipped",
        ),
        (
            "active plan",
            JobStatus.ENCODING,
            JobStage.PLAN,
            AttemptStatus.RUNNING,
            "probe:complete plan:active scene_detect:pending encode:pending validate:pending promote:pending cleanup:skipped",
        ),
        (
            "active scene detect",
            JobStatus.ENCODING,
            JobStage.SCENE_DETECT,
            AttemptStatus.RUNNING,
            "probe:complete plan:complete scene_detect:active encode:pending validate:pending promote:pending cleanup:skipped",
        ),
        (
            "active encode",
            JobStatus.ENCODING,
            JobStage.ENCODE,
            AttemptStatus.RUNNING,
            "probe:complete plan:complete scene_detect:complete encode:active validate:pending promote:pending cleanup:skipped",
        ),
        (
            "encoded waiting for validation",
            JobStatus.ENCODED,
            JobStage.VALIDATE,
            AttemptStatus.COMPLETED,
            "probe:complete plan:complete scene_detect:complete encode:complete validate:pending promote:pending cleanup:skipped",
        ),
        (
            "active validation",
            JobStatus.VALIDATING,
            JobStage.VALIDATE,
            AttemptStatus.RUNNING,
            "probe:complete plan:complete scene_detect:complete encode:complete validate:active promote:pending cleanup:skipped",
        ),
        (
            "ready to promote",
            JobStatus.READY_TO_PROMOTE,
            JobStage.PROMOTE,
            AttemptStatus.COMPLETED,
            "probe:complete plan:complete scene_detect:complete encode:complete validate:complete promote:pending cleanup:skipped",
        ),
        (
            "active promotion",
            JobStatus.PROMOTING,
            JobStage.PROMOTE,
            AttemptStatus.RUNNING,
            "probe:complete plan:complete scene_detect:complete encode:complete validate:complete promote:active cleanup:skipped",
        ),
        (
            "promoted",
            JobStatus.PROMOTED,
            JobStage.PROMOTE,
            AttemptStatus.COMPLETED,
            "probe:complete plan:complete scene_detect:complete encode:complete validate:complete promote:complete cleanup:skipped",
        ),
        (
            "cleanup",
            JobStatus.CLEANING,
            JobStage.CLEANUP,
            AttemptStatus.RUNNING,
            "probe:complete plan:complete scene_detect:complete encode:complete validate:complete promote:complete cleanup:active",
        ),
        (
            "validation failure",
            JobStatus.VALIDATION_FAILED,
            JobStage.VALIDATE,
            AttemptStatus.FAILED,
            "probe:complete plan:complete scene_detect:complete encode:complete validate:failed promote:blocked cleanup:blocked",
        ),
        (
            "size rejection",
            JobStatus.SIZE_REJECTED,
            JobStage.PROMOTE,
            AttemptStatus.COMPLETED,
            "probe:complete plan:complete scene_detect:complete encode:complete validate:complete promote:blocked cleanup:skipped",
        ),
        (
            "cancellation during encode",
            JobStatus.CANCELLED,
            JobStage.ENCODE,
            AttemptStatus.CANCELED,
            "probe:complete plan:complete scene_detect:complete encode:failed validate:blocked promote:blocked cleanup:blocked",
        ),
        (
            "retry attempt",
            JobStatus.ENCODING,
            JobStage.ENCODE,
            AttemptStatus.RUNNING,
            "probe:complete plan:complete scene_detect:complete encode:active validate:pending promote:pending cleanup:skipped",
        ),
        (
            "stage not applicable",
            JobStatus.READY_TO_PROMOTE,
            JobStage.PROMOTE,
            None,
            "probe:complete plan:complete scene_detect:complete encode:complete validate:complete promote:pending cleanup:skipped",
        ),
    ],
)
def test_project_workflow_steps(
    label: str,
    job_status: JobStatus,
    job_stage: JobStage,
    attempt_status: AttemptStatus | None,
    expected: str,
) -> None:
    steps = project_workflow_steps(
        job_status=job_status,
        job_stage=job_stage,
        attempt_status=attempt_status,
    )

    assert _compact(steps) == expected, label


def test_active_probe_and_plan_are_not_labeled_as_encoding() -> None:
    probe = project_workflow_steps(
        job_status=JobStatus.ENCODING,
        job_stage=JobStage.PROBE,
        attempt_status=AttemptStatus.RUNNING,
    )
    plan = project_workflow_steps(
        job_status=JobStatus.ENCODING,
        job_stage=JobStage.PLAN,
        attempt_status=AttemptStatus.RUNNING,
    )
    scene_detect = project_workflow_steps(
        job_status=JobStatus.ENCODING,
        job_stage=JobStage.SCENE_DETECT,
        attempt_status=AttemptStatus.RUNNING,
    )

    assert _state_for(probe, JobStage.PROBE) is WorkflowStepState.ACTIVE
    assert _state_for(probe, JobStage.ENCODE) is WorkflowStepState.PENDING
    assert _state_for(plan, JobStage.PLAN) is WorkflowStepState.ACTIVE
    assert _state_for(plan, JobStage.SCENE_DETECT) is WorkflowStepState.PENDING
    assert _state_for(plan, JobStage.ENCODE) is WorkflowStepState.PENDING
    assert _state_for(scene_detect, JobStage.SCENE_DETECT) is WorkflowStepState.ACTIVE
    assert _state_for(scene_detect, JobStage.ENCODE) is WorkflowStepState.PENDING


def _compact(steps: tuple[object, ...]) -> str:
    return " ".join(f"{step.stage.value}:{step.state.value}" for step in steps)


def _state_for(steps: tuple[object, ...], stage: JobStage) -> WorkflowStepState:
    for step in steps:
        if step.stage == stage:
            return step.state
    raise AssertionError(f"Missing stage {stage}")
