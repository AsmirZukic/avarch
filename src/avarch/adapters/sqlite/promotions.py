from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from avarch.adapters.sqlite.job_transitions import transition_job
from avarch.adapters.sqlite.models import (
    Job,
    JobAttempt,
    MediaFile,
    MediaFileStatus,
    PromotionRecord,
)
from avarch.adapters.sqlite.progress import SqliteProgressStore
from avarch.domain.jobs import (
    AttemptStatus,
    JobOutcomeReason,
    JobStage,
    JobStatus,
    ResourceClass,
)
from avarch.domain.progress import ProgressPhase, ProgressSnapshot, ProgressSource
from avarch.models.promotion import PromotionMode, PromotionPhase, PromotionStatus


class PromotionRecordNotFoundError(LookupError):
    pass


class PromotionLeaseOwnershipError(ValueError):
    pass


class PromotionActiveLeaseError(ValueError):
    pass


class PromotionRecoveryLookupError(LookupError):
    pass


class PromotionClaimPersistenceError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PromotedMediaFileSnapshot:
    size_bytes: int
    mtime_ns: int
    device_id: int
    inode: int
    fs_fingerprint: str


@dataclass(frozen=True, slots=True)
class PromotionClaimData:
    operation_id: str
    job_id: int
    validation_result_id: int
    mode: PromotionMode
    source_path: Path
    validated_output_path: Path
    final_path: Path
    staging_path: Path
    backup_path: Path | None
    source_fingerprint: str
    source_stat_json: str
    validated_output_fingerprint: str
    journal_path: Path
    temp_dir: Path
    owner_token: str
    attempt_number: int
    now: datetime
    lease_seconds: float


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


def require_promotion_record(session: Session, *, promotion_id: int) -> PromotionRecord:
    return _require_promotion_record(session, promotion_id)


def require_promotion_job(session: Session, *, job_id: int) -> Job:
    return _require_job(session, job_id)


def recoverable_promotion(session: Session, *, job_id: int, now: datetime) -> PromotionRecord:
    record = latest_promotion_record(session, job_id=job_id)
    if record is None:
        raise PromotionRecoveryLookupError("No promotion record exists for this job.")
    if record.status == PromotionStatus.COMPLETED:
        raise PromotionRecoveryLookupError("Promotion is already completed.")
    if (
        record.lease_expires_at is not None
        and record.lease_expires_at > now
        and record.owner_token is not None
    ):
        raise PromotionActiveLeaseError("Promotion lease is still active.")
    return record


def claim_recoverable_promotion(
    session: Session,
    *,
    job_id: int,
    owner_token: str,
    now: datetime,
    lease_seconds: float,
) -> PromotionRecord:
    record = recoverable_promotion(session, job_id=job_id, now=now)
    record.owner_token = owner_token
    record.heartbeat_at = now
    record.lease_expires_at = now + timedelta(seconds=lease_seconds)
    record.status = PromotionStatus.RUNNING
    session.add(record)
    return record


def next_promotion_attempt_number(session: Session, *, job: Job, job_id: int) -> int:
    latest_attempt = session.exec(
        select(JobAttempt)
        .where(JobAttempt.job_id == job_id)
        .order_by(col(JobAttempt.attempt_number).desc())
    ).first()
    return (
        max(
            job.attempts,
            latest_attempt.attempt_number if latest_attempt is not None else 0,
        )
        + 1
    )


def persist_promotion_claim(
    session: Session,
    *,
    job: Job,
    claim: PromotionClaimData,
) -> PromotionRecord:
    transition_job(job, JobStatus.PROMOTING, stage=JobStage.PROMOTE, now=claim.now)
    job.claimed_by = claim.owner_token
    job.attempts = claim.attempt_number
    job.started_at = job.started_at or claim.now
    job.finished_at = None
    job.updated_at = claim.now
    attempt = JobAttempt(
        job_id=claim.job_id,
        attempt_number=claim.attempt_number,
        stage=JobStage.PROMOTE,
        resource_class=ResourceClass.FILE_OP,
        status=AttemptStatus.RUNNING,
        runner_id=claim.owner_token,
        started_at=claim.now,
        temp_dir=str(claim.temp_dir),
        output_path=str(claim.final_path),
    )
    session.add(job)
    session.add(attempt)
    session.flush()
    if attempt.id is None:
        raise PromotionClaimPersistenceError("Promotion attempt id was not assigned.")

    record = PromotionRecord(
        operation_id=claim.operation_id,
        job_id=claim.job_id,
        attempt_id=attempt.id,
        validation_result_id=claim.validation_result_id,
        mode=claim.mode,
        status=PromotionStatus.RUNNING,
        phase=PromotionPhase.PREPARED,
        source_path=str(claim.source_path),
        validated_output_path=str(claim.validated_output_path),
        final_path=str(claim.final_path),
        promotion_target_path=str(claim.final_path),
        staging_path=str(claim.staging_path),
        backup_path=str(claim.backup_path) if claim.backup_path is not None else None,
        source_fingerprint_before=claim.source_fingerprint,
        source_stat_json=claim.source_stat_json,
        validated_output_fingerprint=claim.validated_output_fingerprint,
        validated_output_digest=None,
        staging_digest=None,
        final_fingerprint=None,
        final_digest=None,
        journal_path=str(claim.journal_path),
        owner_token=claim.owner_token,
        heartbeat_at=claim.now,
        lease_expires_at=claim.now + timedelta(seconds=claim.lease_seconds),
        created_at=claim.now,
        updated_at=claim.now,
        started_at=claim.now,
    )
    session.add(record)
    session.flush()
    if record.id is None:
        raise PromotionClaimPersistenceError("Promotion record id was not assigned.")
    SqliteProgressStore(session).save_snapshot(
        attempt_id=attempt.id,
        snapshot=_promotion_progress_snapshot(ProgressPhase.PROMOTING, now=claim.now),
        persisted_at=claim.now,
    )
    job.latest_promotion_id = record.id
    session.add(job)
    return record


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


def set_promotion_phase(
    engine: Engine,
    *,
    promotion_id: int,
    phase: PromotionPhase,
    owner_token: str,
    now: datetime,
) -> None:
    with Session(engine) as session, session.begin():
        record = session.get(PromotionRecord, promotion_id)
        if record is None:
            raise PromotionRecordNotFoundError(f"Promotion record not found: {promotion_id}")
        if record.owner_token != owner_token:
            raise PromotionLeaseOwnershipError("Promotion lease belongs to another owner.")
        record.phase = phase
        record.updated_at = now
        session.add(record)


def update_promotion_staged(
    session: Session,
    *,
    promotion_id: int,
    source_digest: str,
    staging_digest: str,
    now: datetime,
) -> None:
    record = _require_promotion_record(session, promotion_id)
    record.validated_output_digest = source_digest
    record.staging_digest = staging_digest
    record.phase = PromotionPhase.STAGED
    record.updated_at = now
    session.add(record)


def update_promotion_verified(
    session: Session,
    *,
    promotion_id: int,
    final_digest: str,
    final_fingerprint: str,
    now: datetime,
) -> None:
    record = _require_promotion_record(session, promotion_id)
    record.final_digest = final_digest
    record.final_fingerprint = final_fingerprint
    record.phase = PromotionPhase.VERIFIED
    record.updated_at = now
    session.add(record)


def update_promotion_cleanup(
    session: Session,
    *,
    promotion_id: int,
    cleanup_error: str | None,
    now: datetime,
) -> PromotionRecord:
    record = _require_promotion_record(session, promotion_id)
    if cleanup_error is None:
        record.cleanup_completed = True
        record.phase = PromotionPhase.CLEANUP_COMPLETE
    else:
        record.cleanup_completed = False
        record.cleanup_error = cleanup_error
    record.updated_at = now
    session.add(record)
    session.flush()
    session.refresh(record)
    return record.model_copy(deep=True)


def mark_promotion_rolled_back(
    session: Session,
    *,
    promotion_id: int,
    now: datetime,
) -> None:
    record = _require_promotion_record(session, promotion_id)
    job = _require_job(session, record.job_id)
    attempt = _require_attempt(session, record.attempt_id)
    record.status = PromotionStatus.ROLLED_BACK
    record.phase = PromotionPhase.ROLLED_BACK
    record.finished_at = now
    record.updated_at = now
    record.owner_token = None
    record.lease_expires_at = None
    attempt.status = AttemptStatus.INTERRUPTED
    attempt.finished_at = now
    transition_job(job, JobStatus.READY_TO_PROMOTE, stage=JobStage.PROMOTE, now=now)
    job.claimed_by = None
    job.finished_at = None
    job.updated_at = now
    SqliteProgressStore(session).finalize_snapshot(
        attempt_id=attempt.id or record.attempt_id,
        snapshot=_promotion_progress_snapshot(ProgressPhase.CANCELLED, now=now),
        persisted_at=now,
    )
    session.add(record)
    session.add(attempt)
    session.add(job)


def mark_promotion_failed_or_validated(
    session: Session,
    *,
    promotion_id: int,
    error: Exception,
    can_return_to_promote: bool,
    now: datetime,
) -> None:
    record = _require_promotion_record(session, promotion_id)
    job = _require_job(session, record.job_id)
    attempt = _require_attempt(session, record.attempt_id)
    record.error_type = error.__class__.__name__
    record.error_message = str(error)
    record.status = (
        PromotionStatus.ROLLED_BACK
        if PromotionStatus(record.status) == PromotionStatus.ROLLED_BACK
        else PromotionStatus.FAILED
    )
    record.phase = (
        PromotionPhase.ROLLED_BACK
        if PromotionPhase(record.phase) == PromotionPhase.ROLLED_BACK
        else PromotionPhase.FAILED
    )
    record.finished_at = now
    record.updated_at = now
    record.owner_token = None
    record.lease_expires_at = None
    attempt.status = AttemptStatus.FAILED
    attempt.error_type = error.__class__.__name__
    attempt.error_message = str(error)
    attempt.finished_at = now
    if can_return_to_promote:
        transition_job(job, JobStatus.READY_TO_PROMOTE, stage=JobStage.PROMOTE, now=now)
        job.finished_at = None
    else:
        transition_job(
            job,
            JobStatus.FAILED,
            reason=JobOutcomeReason.FAILED_PROMOTION,
            stage=JobStage.PROMOTE,
            now=now,
        )
        job.finished_at = now
    job.claimed_by = None
    job.last_error_type = error.__class__.__name__
    job.last_error_message = str(error)
    job.updated_at = now
    SqliteProgressStore(session).finalize_snapshot(
        attempt_id=attempt.id or record.attempt_id,
        snapshot=_promotion_progress_snapshot(
            ProgressPhase.FAILED,
            now=now,
            message=str(error),
        ),
        persisted_at=now,
    )
    session.add(record)
    session.add(attempt)
    session.add(job)


def commit_verified_promotion(
    session: Session,
    *,
    record: PromotionRecord,
    installed_path: Path,
    final_fingerprint: str,
    media_snapshot: PromotedMediaFileSnapshot | None,
    now: datetime,
) -> None:
    job = _require_job(session, record.job_id)
    attempt = _require_attempt(session, record.attempt_id)
    record.status = PromotionStatus.COMPLETED
    record.phase = PromotionPhase.COMMITTED
    record.final_fingerprint = final_fingerprint
    record.finished_at = now
    record.updated_at = now
    record.owner_token = None
    record.lease_expires_at = None
    attempt.status = AttemptStatus.COMPLETED
    attempt.finished_at = now
    attempt.output_path = str(installed_path)
    job.latest_promotion_id = record.id
    transition_job(
        job,
        JobStatus.PROMOTED,
        reason=JobOutcomeReason.SUCCESS,
        stage=JobStage.PROMOTE,
        now=now,
    )
    job.claimed_by = None
    job.last_error_type = None
    job.last_error_message = None
    job.finished_at = now
    job.updated_at = now
    if media_snapshot is not None:
        media_file = session.get(MediaFile, job.media_file_id)
        if media_file is None:
            raise PromotionRecordNotFoundError("Promoted job media file no longer exists.")
        media_file.size_bytes = media_snapshot.size_bytes
        media_file.mtime_ns = media_snapshot.mtime_ns
        media_file.device_id = media_snapshot.device_id
        media_file.inode = media_snapshot.inode
        media_file.fs_fingerprint = media_snapshot.fs_fingerprint
        media_file.last_seen_at = now
        media_file.status = MediaFileStatus.PRESENT
        media_file.latest_probe_id = None
        session.add(media_file)
    SqliteProgressStore(session).finalize_snapshot(
        attempt_id=attempt.id or record.attempt_id,
        snapshot=_promotion_progress_snapshot(ProgressPhase.COMPLETED, now=now),
        persisted_at=now,
    )
    session.add(record)
    session.add(attempt)
    session.add(job)


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


def _promotion_progress_snapshot(
    phase: ProgressPhase,
    *,
    now: datetime,
    message: str | None = None,
) -> ProgressSnapshot:
    return ProgressSnapshot(
        phase=phase,
        current=None,
        total=None,
        unit=None,
        rate_per_second=None,
        speed_ratio=None,
        source=ProgressSource.SCHEDULER,
        message=_sanitize_progress_message(message),
        phase_started_at=now,
        observed_at=now,
        heartbeat_at=now,
        advanced_at=None,
    )


def _sanitize_progress_message(message: str | None, *, max_length: int = 500) -> str | None:
    if message is None:
        return None
    sanitized = " ".join(message.split())
    if len(sanitized) <= max_length:
        return sanitized
    return sanitized[: max_length - 3].rstrip() + "..."


def _require_promotion_record(session: Session, promotion_id: int) -> PromotionRecord:
    record = session.get(PromotionRecord, promotion_id)
    if record is None:
        raise PromotionRecordNotFoundError(f"Promotion record not found: {promotion_id}")
    return record


def _require_job(session: Session, job_id: int) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise PromotionRecordNotFoundError(f"Job not found: {job_id}")
    return job


def _require_attempt(session: Session, attempt_id: int) -> JobAttempt:
    attempt = session.get(JobAttempt, attempt_id)
    if attempt is None:
        raise PromotionRecordNotFoundError(f"Promotion attempt not found: {attempt_id}")
    return attempt


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
