from __future__ import annotations

from datetime import datetime

from sqlmodel import Session

from avarch.adapters.sqlite.models import Job
from avarch.domain.jobs import JobStatus
from avarch.models.promotion import PromotionApproval, PromotionMode


class PromotionApprovalError(ValueError):
    pass


def approve_job_promotion(
    session: Session,
    *,
    job: Job,
    mode: PromotionMode,
    actor: str,
    now: datetime,
    delete_backup: bool = False,
) -> None:
    if job.plan_hash is None:
        raise PromotionApprovalError("Cannot approve a job without a persisted plan.")
    if job.status in {JobStatus.PROMOTING, JobStatus.PROMOTED}:
        raise PromotionApprovalError("Cannot change approval during or after promotion.")
    if delete_backup and mode != PromotionMode.REPLACE_ATOMIC:
        raise PromotionApprovalError("Backup deletion requires replace-atomic mode.")
    job.promotion_approval_json = PromotionApproval(
        mode=mode,
        plan_hash=job.plan_hash,
        source_fingerprint=job.source_fs_fingerprint,
        approved_at=now,
        approved_by=actor,
        delete_backup=delete_backup,
    ).model_dump_json()
    session.add(job)


def current_promotion_approval(job: Job) -> PromotionApproval | None:
    if job.promotion_approval_json is None:
        return None
    try:
        approval = PromotionApproval.model_validate_json(job.promotion_approval_json)
    except ValueError as exc:
        raise PromotionApprovalError("Stored promotion approval is invalid.") from exc
    if (
        approval.plan_hash != job.plan_hash
        or approval.source_fingerprint != job.source_fs_fingerprint
    ):
        raise PromotionApprovalError("Promotion approval is stale; approve the current job again.")
    return approval


def require_replacement_approval(job: Job, mode: PromotionMode) -> PromotionApproval | None:
    approval = current_promotion_approval(job)
    if mode != PromotionMode.KEEP_ORIGINAL and (approval is None or approval.mode != mode):
        raise PromotionApprovalError("Replacement requires approval stored with this specific job.")
    return approval
