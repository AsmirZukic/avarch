from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from avarch.application.job_views import WorkflowJobItem
from avarch.domain.jobs import JobStatus, job_has_passed_validation


@dataclass(frozen=True, slots=True)
class WorkflowPromotionReadiness:
    promotable_jobs: tuple[WorkflowJobItem, ...]
    failed_jobs: tuple[WorkflowJobItem, ...]
    blocked_jobs: tuple[WorkflowJobItem, ...]


def verify_workflow_jobs(jobs: Sequence[WorkflowJobItem]) -> WorkflowPromotionReadiness:
    promotable_jobs = tuple(
        job
        for job in jobs
        if job_has_passed_validation(job.status, job.stage) and job.id is not None
    )
    failed_jobs = tuple(
        job for job in jobs if job.status in {JobStatus.FAILED, JobStatus.VALIDATION_FAILED}
    )
    blocked_jobs = tuple(
        job
        for job in jobs
        if job.status
        not in {
            JobStatus.READY_TO_PROMOTE,
            JobStatus.PROMOTED,
            JobStatus.SKIPPED,
            JobStatus.SIZE_REJECTED,
        }
        and job.status not in {JobStatus.FAILED, JobStatus.VALIDATION_FAILED}
    )

    return WorkflowPromotionReadiness(
        promotable_jobs=promotable_jobs,
        failed_jobs=failed_jobs,
        blocked_jobs=blocked_jobs,
    )
