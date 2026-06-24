from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from sqlmodel import Session, col, select

from avarch.adapters.sqlite.models import Job, PromotionRecord
from avarch.models.promotion import PromotionStatus


class PromotionRecordNotFoundError(LookupError):
    pass


class PromotionLeaseOwnershipError(ValueError):
    pass


def has_completed_promotion(session: Session, job: Job) -> bool:
    if job.id is None:
        return False
    return (
        session.exec(
            select(PromotionRecord).where(
                PromotionRecord.job_id == job.id,
                PromotionRecord.status == PromotionStatus.COMPLETED,
            )
        ).first()
        is not None
    )


def latest_promotion_record(session: Session, *, job_id: int) -> PromotionRecord | None:
    return session.exec(
        select(PromotionRecord)
        .where(PromotionRecord.job_id == job_id)
        .order_by(col(PromotionRecord.created_at).desc(), col(PromotionRecord.id).desc())
    ).first()


def renew_promotion_lease(
    session: Session,
    *,
    promotion_id: int,
    owner_token: str,
    now: datetime,
    lease_seconds: float,
) -> None:
    record = session.get(PromotionRecord, promotion_id)
    if record is None:
        raise PromotionRecordNotFoundError(f"Promotion record not found: {promotion_id}")
    if record.owner_token != owner_token:
        raise PromotionLeaseOwnershipError("Promotion lease belongs to another owner.")
    if record.status == PromotionStatus.COMPLETED:
        return
    record.heartbeat_at = now
    record.lease_expires_at = now + timedelta(seconds=lease_seconds)
    record.updated_at = now
    session.add(record)


def has_active_promotion_lease(session: Session, *, job_id: int, now: datetime) -> bool:
    return (
        session.exec(
            select(PromotionRecord).where(
                PromotionRecord.job_id == job_id,
                PromotionRecord.status == PromotionStatus.RUNNING,
                col(PromotionRecord.lease_expires_at).is_not(None),
                col(PromotionRecord.lease_expires_at) > now,
            )
        ).first()
        is not None
    )


def has_active_target_lease(
    session: Session,
    *,
    job_id: int,
    target_path: Path,
    now: datetime,
) -> bool:
    return (
        session.exec(
            select(PromotionRecord).where(
                PromotionRecord.job_id != job_id,
                PromotionRecord.status == PromotionStatus.RUNNING,
                PromotionRecord.promotion_target_path == str(target_path),
                col(PromotionRecord.lease_expires_at).is_not(None),
                col(PromotionRecord.lease_expires_at) > now,
            )
        ).first()
        is not None
    )
