from __future__ import annotations

from avarch.application.job_views import WorkflowJobItem
from avarch.application.workflow_run import verify_workflow_jobs
from avarch.domain.jobs import JobStage, JobStatus


def test_verify_workflow_jobs_classifies_promotion_readiness() -> None:
    promotable_job = _workflow_job(
        id=10,
        status=JobStatus.READY_TO_PROMOTE,
        stage=JobStage.PROMOTE,
    )
    failed_job = _workflow_job(id=11, status=JobStatus.FAILED, stage=JobStage.ENCODE)
    blocked_job = _workflow_job(id=12, status=JobStatus.ENCODED, stage=JobStage.VALIDATE)
    promoted_job = _workflow_job(id=13, status=JobStatus.PROMOTED, stage=JobStage.PROMOTE)
    missing_id_job = _workflow_job(
        id=None,
        status=JobStatus.READY_TO_PROMOTE,
        stage=JobStage.PROMOTE,
    )

    readiness = verify_workflow_jobs(
        [promotable_job, failed_job, blocked_job, promoted_job, missing_id_job]
    )

    assert readiness.promotable_jobs == (promotable_job,)
    assert readiness.failed_jobs == (failed_job,)
    assert readiness.blocked_jobs == (blocked_job,)


def test_verify_workflow_jobs_requires_promote_stage_for_promotable_jobs() -> None:
    job = _workflow_job(
        id=20,
        status=JobStatus.READY_TO_PROMOTE,
        stage=JobStage.VALIDATE,
    )

    readiness = verify_workflow_jobs([job])

    assert readiness.promotable_jobs == ()
    assert readiness.failed_jobs == ()
    assert readiness.blocked_jobs == ()


def _workflow_job(
    *,
    id: int | None,
    status: JobStatus,
    stage: JobStage,
) -> WorkflowJobItem:
    return WorkflowJobItem(
        id=id,
        status=status,
        stage=stage,
        output_path=f"/tmp/{id or 'missing'}.mkv",
        plan_hash=f"plan-{id or 'missing'}",
    )
