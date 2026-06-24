from __future__ import annotations

import asyncio
import errno
import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

from pydantic import BaseModel
from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from avarch.adapters.filesystem.plans import PlanArtifactLoadError, load_plan_artifact
from avarch.adapters.filesystem.promotion import (
    PROMOTION_DIGEST_CHUNK_SIZE,
    calculate_promotion_digest,
    create_promotion_digest,
    fsync_directory,
    write_promotion_journal,
)
from avarch.adapters.sqlite.db import create_db_engine
from avarch.adapters.sqlite.job_transitions import transition_job
from avarch.adapters.sqlite.models import (
    Job,
    JobAttempt,
    MediaFile,
    MediaFileStatus,
    PromotionRecord,
    ValidationResult,
)
from avarch.adapters.sqlite.promotions import (
    PromotionLeaseOwnershipError,
    PromotionRecordNotFoundError,
    has_active_promotion_lease,
    has_active_target_lease,
    has_completed_promotion,
    latest_promotion_record,
    renew_promotion_lease,
)
from avarch.adapters.sqlite.validations import latest_validation
from avarch.config import AppConfig
from avarch.domain.jobs import (
    AttemptStatus,
    JobOutcomeReason,
    JobStage,
    JobStatus,
    ResourceClass,
)
from avarch.domain.promotion import (
    PromotionPathConflictError,
    derive_backup_path,
    derive_keep_original_path,
    derive_promotion_journal_path,
    derive_staging_path,
)
from avarch.models.plan import TranscodePlan
from avarch.models.promotion import (
    FileStatSnapshot,
    PromotionJournal,
    PromotionMode,
    PromotionPhase,
    PromotionStatus,
)
from avarch.scanner import create_file_snapshot
from avarch.serialization import canonical_json

PROMOTION_LEASE_SECONDS = 30.0
PROMOTION_HEARTBEAT_SECONDS = 5.0
PROMOTION_FREE_SPACE_RESERVE_BYTES = 64 * 1024 * 1024


class PromotionError(RuntimeError):
    pass


class PromotionEligibilityError(PromotionError):
    pass


class PromotionConflictError(PromotionError):
    pass


class PromotionLeaseError(PromotionError):
    pass


class PromotionFilesystemError(PromotionError):
    pass


class PromotionVerificationError(PromotionError):
    pass


class PromotionRecoveryError(PromotionError):
    pass


class PromotionRollbackError(PromotionError):
    pass


class PromotionPersistenceError(PromotionError):
    pass


class PromotionPreflightResult(BaseModel):
    job_id: int
    validation_result_id: int

    mode: PromotionMode

    source_path: Path
    validated_output_path: Path

    final_path: Path
    staging_path: Path
    backup_path: Path | None

    source_fingerprint: str
    source_stat: FileStatSnapshot

    validated_output_fingerprint: str
    validated_output_size: int

    destination_free_bytes: int
    required_free_bytes: int

    warnings: list[str]


@dataclass(frozen=True, slots=True)
class PromotionResult:
    job_id: int
    promotion_id: int
    status: PromotionStatus
    final_path: Path
    promoted: bool
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class StagedOutput:
    source_digest: str
    staging_digest: str
    size_bytes: int


class PromotionHeartbeat:
    def __init__(self, *, config: AppConfig, promotion_id: int, owner_token: str) -> None:
        self.config = config
        self.promotion_id = promotion_id
        self.owner_token = owner_token
        self.last_heartbeat = datetime.min.replace(tzinfo=UTC)

    def refresh(self, *, force: bool = False) -> None:
        now = _utc_now()
        if not force and (now - self.last_heartbeat).total_seconds() < PROMOTION_HEARTBEAT_SECONDS:
            return
        engine = create_db_engine(self.config.database.url)
        with Session(engine) as session, session.begin():
            try:
                renew_promotion_lease(
                    session,
                    promotion_id=self.promotion_id,
                    owner_token=self.owner_token,
                    now=now,
                    lease_seconds=PROMOTION_LEASE_SECONDS,
                )
            except PromotionRecordNotFoundError as exc:
                raise PromotionPersistenceError(str(exc)) from exc
            except PromotionLeaseOwnershipError as exc:
                raise PromotionLeaseError(str(exc)) from exc
        self.last_heartbeat = now


def create_promotion_staging_file(
    path: Path,
    *,
    mode: int,
) -> BinaryIO:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, mode & 0o777)
    except FileExistsError as exc:
        raise PromotionConflictError(f"Staging path already exists: {path}") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PromotionConflictError(f"Staging path is a symlink: {path}") from exc
        raise PromotionFilesystemError(f"Unable to create staging file: {path}") from exc
    return os.fdopen(fd, "wb")


async def stage_validated_output(
    *,
    source_output: Path,
    staging_path: Path,
    expected_fingerprint: str,
    expected_mode: int,
    heartbeat: PromotionHeartbeat,
) -> StagedOutput:
    return await asyncio.to_thread(
        _stage_validated_output_sync,
        source_output=source_output,
        staging_path=staging_path,
        expected_fingerprint=expected_fingerprint,
        expected_mode=expected_mode,
        heartbeat=heartbeat,
    )


def _stage_validated_output_sync(
    *,
    source_output: Path,
    staging_path: Path,
    expected_fingerprint: str,
    expected_mode: int,
    heartbeat: PromotionHeartbeat,
) -> StagedOutput:
    before = create_file_snapshot(source_output)
    if before.fs_fingerprint != expected_fingerprint:
        raise PromotionEligibilityError("Validated output fingerprint changed before staging.")

    digest = create_promotion_digest()
    bytes_copied = 0
    try:
        with (
            source_output.open("rb") as input_file,
            create_promotion_staging_file(
                staging_path,
                mode=expected_mode,
            ) as output_file,
        ):
            while chunk := input_file.read(PROMOTION_DIGEST_CHUNK_SIZE):
                output_file.write(chunk)
                digest.update(chunk)
                bytes_copied += len(chunk)
                heartbeat.refresh()
            output_file.flush()
            os.fsync(output_file.fileno())
        fsync_directory(staging_path.parent)
    except Exception:
        _unlink_owned_staging(staging_path)
        raise

    after = create_file_snapshot(source_output)
    if before.fs_fingerprint != after.fs_fingerprint:
        _unlink_owned_staging(staging_path)
        raise PromotionVerificationError("Validated output changed during staging.")

    source_digest = digest.hexdigest()
    staging_digest = calculate_promotion_digest(staging_path)
    if source_digest != staging_digest:
        _unlink_owned_staging(staging_path)
        raise PromotionVerificationError("Staging digest does not match validated output.")
    return StagedOutput(
        source_digest=source_digest,
        staging_digest=staging_digest,
        size_bytes=bytes_copied,
    )


def create_original_rollback_link(
    *,
    source_path: Path,
    backup_path: Path,
    expected_source: FileStatSnapshot,
) -> None:
    _reject_symlink(source_path, "Source path")
    if backup_path.exists() or backup_path.is_symlink():
        raise PromotionConflictError(f"Backup path already exists: {backup_path}")
    source_stat = _stat_snapshot(source_path)
    if source_stat != expected_source:
        raise PromotionVerificationError("Source changed before rollback backup creation.")
    try:
        os.link(source_path, backup_path, follow_symlinks=False)
    except OSError as exc:
        raise PromotionFilesystemError("Unable to create hard-link rollback backup.") from exc
    fsync_directory(source_path.parent)
    _verify_hardlink_backup(
        source_path=source_path,
        backup_path=backup_path,
        expected_source=expected_source,
    )


def validate_promotion_preflight(
    *,
    job: Job,
    plan: TranscodePlan,
    validation: ValidationResult,
    mode: PromotionMode,
    operation_id: str = "preview",
) -> PromotionPreflightResult:
    if job.id is None:
        raise PromotionEligibilityError("Job must be persisted before promotion.")
    if validation.id is None:
        raise PromotionEligibilityError("Validation result must be persisted before promotion.")
    if job.status != JobStatus.VALIDATED or job.stage != JobStage.PROMOTE:
        raise PromotionEligibilityError("Job is not validated and ready for promotion.")
    if job.latest_validation_id != validation.id:
        raise PromotionEligibilityError("Validation result is not the latest job validation.")
    if not validation.passed:
        raise PromotionEligibilityError("Validation result did not pass.")
    if validation.job_id != job.id:
        raise PromotionEligibilityError("Validation result belongs to another job.")
    if validation.plan_hash != job.plan_hash or validation.plan_hash != plan.plan_hash:
        raise PromotionEligibilityError("Validation result does not match the job plan.")
    if (
        validation.output_path != job.output_path
        or Path(validation.output_path) != plan.output_path
    ):
        raise PromotionEligibilityError("Validation result does not match the planned output.")
    if mode.value not in plan.promotion.allowed_modes:
        raise PromotionEligibilityError(f"Promotion mode is not allowed by the plan: {mode.value}")

    source_path = plan.input_path
    output_path = plan.output_path
    _require_regular_nonsymlink(source_path, "Source path")
    _require_regular_nonsymlink(output_path, "Validated output")

    source_snapshot = create_file_snapshot(source_path)
    if source_snapshot.fs_fingerprint != plan.source_fs_fingerprint:
        raise PromotionEligibilityError("Source fingerprint no longer matches the plan.")

    output_snapshot = create_file_snapshot(output_path)
    if output_snapshot.fs_fingerprint != validation.output_fs_fingerprint:
        raise PromotionEligibilityError("Validated output fingerprint changed.")

    if mode == PromotionMode.KEEP_ORIGINAL:
        try:
            final_path = derive_keep_original_path(
                source_path,
                container=plan.execution_identity.final_container,
            )
        except PromotionPathConflictError as exc:
            raise PromotionConflictError(str(exc)) from exc
        backup_path = None
    else:
        final_path = source_path
        backup_path = derive_backup_path(source_path)
        if backup_path.exists() or backup_path.is_symlink():
            raise PromotionConflictError(f"Backup path already exists: {backup_path}")

    if mode == PromotionMode.KEEP_ORIGINAL and (final_path.exists() or final_path.is_symlink()):
        raise PromotionConflictError(f"Final path already exists: {final_path}")
    if not final_path.parent.exists() or final_path.parent.is_symlink():
        raise PromotionEligibilityError(
            f"Destination parent is not a real directory: {final_path.parent}"
        )

    staging_path = derive_staging_path(final_path, operation_id=operation_id)
    if staging_path.exists() or staging_path.is_symlink():
        raise PromotionConflictError(f"Staging path already exists: {staging_path}")

    free = shutil.disk_usage(final_path.parent).free
    required = output_snapshot.size_bytes + PROMOTION_FREE_SPACE_RESERVE_BYTES
    if free < required:
        raise PromotionFilesystemError("Insufficient free space for destination-local staging.")

    return PromotionPreflightResult(
        job_id=job.id,
        validation_result_id=validation.id,
        mode=mode,
        source_path=source_path,
        validated_output_path=output_path,
        final_path=final_path,
        staging_path=staging_path,
        backup_path=backup_path,
        source_fingerprint=source_snapshot.fs_fingerprint,
        source_stat=_stat_snapshot(source_path),
        validated_output_fingerprint=output_snapshot.fs_fingerprint,
        validated_output_size=output_snapshot.size_bytes,
        destination_free_bytes=free,
        required_free_bytes=required,
        warnings=[],
    )


def claim_promotion(
    session: Session,
    *,
    job_id: int,
    mode: PromotionMode,
    owner_token: str,
    now: datetime,
) -> PromotionRecord:
    job = _require_job(session, job_id)
    if job.status != JobStatus.VALIDATED or job.stage != JobStage.PROMOTE:
        raise PromotionEligibilityError("Job is not validated and ready for promotion.")
    if has_completed_promotion(session, job):
        raise PromotionEligibilityError("Job already has a completed promotion.")
    if has_active_promotion_lease(session, job_id=job_id, now=now):
        raise PromotionLeaseError("Another promotion lease is still active.")
    plan = _load_job_plan(job)
    validation = latest_validation(session, job)
    if validation is None:
        raise PromotionEligibilityError("Job has no current validation result.")
    operation_id = uuid.uuid4().hex
    preflight = validate_promotion_preflight(
        job=job,
        plan=plan,
        validation=validation,
        mode=mode,
        operation_id=operation_id,
    )
    if has_active_target_lease(session, job_id=job_id, target_path=preflight.final_path, now=now):
        raise PromotionLeaseError(f"Promotion target is already locked: {preflight.final_path}")

    latest_attempt = session.exec(
        select(JobAttempt)
        .where(JobAttempt.job_id == job_id)
        .order_by(col(JobAttempt.attempt_number).desc())
    ).first()
    next_attempt_number = (
        max(
            job.attempts,
            latest_attempt.attempt_number if latest_attempt is not None else 0,
        )
        + 1
    )

    transition_job(job, JobStatus.PROMOTING, now=now)
    job.stage = JobStage.PROMOTE
    job.claimed_by = owner_token
    job.attempts = next_attempt_number
    job.started_at = job.started_at or now
    job.finished_at = None
    job.updated_at = now
    attempt = JobAttempt(
        job_id=job_id,
        attempt_number=next_attempt_number,
        stage=JobStage.PROMOTE,
        resource_class=ResourceClass.FILE_OP,
        status=AttemptStatus.RUNNING,
        runner_id=owner_token,
        started_at=now,
        temp_dir=str(plan.temp_dir),
        output_path=str(preflight.final_path),
    )
    session.add(job)
    session.add(attempt)
    session.flush()
    if attempt.id is None:
        raise PromotionPersistenceError("Promotion attempt id was not assigned.")

    record = PromotionRecord(
        operation_id=operation_id,
        job_id=job_id,
        attempt_id=attempt.id,
        validation_result_id=preflight.validation_result_id,
        mode=mode,
        status=PromotionStatus.RUNNING,
        phase=PromotionPhase.PREPARED,
        source_path=str(preflight.source_path),
        validated_output_path=str(preflight.validated_output_path),
        final_path=str(preflight.final_path),
        promotion_target_path=str(preflight.final_path),
        staging_path=str(preflight.staging_path),
        backup_path=str(preflight.backup_path) if preflight.backup_path is not None else None,
        source_fingerprint_before=preflight.source_fingerprint,
        source_stat_json=canonical_json(preflight.source_stat),
        validated_output_fingerprint=preflight.validated_output_fingerprint,
        validated_output_digest=None,
        staging_digest=None,
        final_fingerprint=None,
        final_digest=None,
        journal_path=str(derive_promotion_journal_path(plan.runtime.runtime_dir)),
        owner_token=owner_token,
        heartbeat_at=now,
        lease_expires_at=now + timedelta(seconds=PROMOTION_LEASE_SECONDS),
        created_at=now,
        updated_at=now,
        started_at=now,
    )
    session.add(record)
    session.flush()
    if record.id is None:
        raise PromotionPersistenceError("Promotion record id was not assigned.")
    job.latest_promotion_id = record.id
    session.add(job)
    return record


async def execute_promotion(
    *,
    job_id: int,
    mode: PromotionMode,
    config: AppConfig,
    owner_token: str,
) -> PromotionRecord:
    engine = create_db_engine(config.database.url)
    now = _utc_now()
    with Session(engine) as session, session.begin():
        record = claim_promotion(
            session,
            job_id=job_id,
            mode=mode,
            owner_token=owner_token,
            now=now,
        )
        promotion_id = _require_id(record)

    try:
        return await _execute_claimed_promotion(
            promotion_id=promotion_id,
            config=config,
            owner_token=owner_token,
        )
    except Exception as exc:
        with Session(engine) as session, session.begin():
            mark_promotion_failed_or_validated(
                session,
                promotion_id=promotion_id,
                error=exc,
                now=_utc_now(),
            )
        raise


async def promote_job(
    job_id: int,
    *,
    config: AppConfig,
    mode: PromotionMode = PromotionMode.REPLACE_ATOMIC,
    owner_token: str | None = None,
) -> PromotionResult:
    token = owner_token or uuid.uuid4().hex
    try:
        record = await execute_promotion(
            job_id=job_id,
            mode=mode,
            config=config,
            owner_token=token,
        )
    except PromotionError as exc:
        return PromotionResult(
            job_id=job_id,
            promotion_id=0,
            status=PromotionStatus.FAILED,
            final_path=Path(),
            promoted=False,
            error_message=str(exc),
        )
    return PromotionResult(
        job_id=job_id,
        promotion_id=_require_id(record),
        status=PromotionStatus(record.status),
        final_path=Path(record.final_path),
        promoted=PromotionStatus(record.status) == PromotionStatus.COMPLETED,
        error_message=record.error_message,
    )


async def recover_promotion(
    *,
    job_id: int,
    config: AppConfig,
    owner_token: str,
) -> PromotionRecord:
    engine = create_db_engine(config.database.url)
    with Session(engine) as session, session.begin():
        record = _recoverable_promotion(session, job_id=job_id, now=_utc_now())
        record.owner_token = owner_token
        record.heartbeat_at = _utc_now()
        record.lease_expires_at = _utc_now() + timedelta(seconds=PROMOTION_LEASE_SECONDS)
        record.status = PromotionStatus.RUNNING
        session.add(record)
        promotion_id = _require_id(record)
    return await _execute_claimed_promotion(
        promotion_id=promotion_id,
        config=config,
        owner_token=owner_token,
    )


async def _execute_claimed_promotion(
    *,
    promotion_id: int,
    config: AppConfig,
    owner_token: str,
) -> PromotionRecord:
    engine = create_db_engine(config.database.url)
    heartbeat = PromotionHeartbeat(
        config=config,
        promotion_id=promotion_id,
        owner_token=owner_token,
    )
    with Session(engine) as session:
        record = _require_promotion(session, promotion_id)
        plan = _load_job_plan(_require_job(session, record.job_id))
        source_path = Path(record.source_path)
        output_path = Path(record.validated_output_path)
        final_path = Path(record.final_path)
        staging_path = Path(record.staging_path)
        backup_path = Path(record.backup_path) if record.backup_path is not None else None
        source_stat = FileStatSnapshot.model_validate_json(record.source_stat_json)
        promotion_mode = PromotionMode(record.mode)
        promotion_phase = _status_phase(record.phase)
        validated_output_fingerprint = record.validated_output_fingerprint
        expected_mode = source_stat.mode

    if staging_path.exists() and promotion_phase in {
        PromotionPhase.PREPARED,
        PromotionPhase.STAGING,
        PromotionPhase.FAILED,
    }:
        _unlink_owned_staging(staging_path)
    if not staging_path.exists():
        _set_phase(engine, promotion_id, PromotionPhase.STAGING, owner_token)
        _write_journal_from_record(engine, promotion_id)
        staged = await stage_validated_output(
            source_output=output_path,
            staging_path=staging_path,
            expected_fingerprint=validated_output_fingerprint,
            expected_mode=expected_mode,
            heartbeat=heartbeat,
        )
        with Session(engine) as session, session.begin():
            record = _require_promotion(session, promotion_id)
            record.validated_output_digest = staged.source_digest
            record.staging_digest = staged.staging_digest
            record.phase = PromotionPhase.STAGED
            record.updated_at = _utc_now()
            session.add(record)
        _write_journal_from_record(engine, promotion_id)

    if backup_path is not None and not backup_path.exists():
        _set_phase(engine, promotion_id, PromotionPhase.BACKUP_PENDING, owner_token)
        _write_journal_from_record(engine, promotion_id)
        create_original_rollback_link(
            source_path=source_path,
            backup_path=backup_path,
            expected_source=source_stat,
        )
        _set_phase(engine, promotion_id, PromotionPhase.BACKUP_CREATED, owner_token)
        _write_journal_from_record(engine, promotion_id)

    install_path = final_path
    if promotion_mode != PromotionMode.KEEP_ORIGINAL:
        install_path = source_path
    _set_phase(engine, promotion_id, PromotionPhase.INSTALL_PENDING, owner_token)
    _write_journal_from_record(engine, promotion_id)
    if staging_path.exists():
        os.replace(staging_path, install_path)
        fsync_directory(install_path.parent)
    _set_phase(engine, promotion_id, PromotionPhase.FINAL_INSTALLED, owner_token)

    final_digest = calculate_promotion_digest(install_path)
    with Session(engine) as session:
        record = _require_promotion(session, promotion_id)
        expected_digest = record.validated_output_digest
    if expected_digest is None or final_digest != expected_digest:
        await _rollback_before_commit(
            promotion_id=promotion_id,
            config=config,
            owner_token=owner_token,
        )
        raise PromotionVerificationError("Final digest does not match validated output.")
    final_snapshot = create_file_snapshot(install_path)
    with Session(engine) as session, session.begin():
        record = _require_promotion(session, promotion_id)
        record.final_digest = final_digest
        record.final_fingerprint = final_snapshot.fs_fingerprint
        record.phase = PromotionPhase.VERIFIED
        record.updated_at = _utc_now()
        session.add(record)
    _write_journal_from_record(engine, promotion_id)

    with Session(engine) as session, session.begin():
        record = _require_promotion(session, promotion_id)
        _commit_verified_promotion(session, record=record, now=_utc_now())
        promotion_id = _require_id(record)

    cleanup_error = _cleanup_after_success(engine, promotion_id, plan)
    with Session(engine) as session, session.begin():
        record = _require_promotion(session, promotion_id)
        if cleanup_error is None:
            record.cleanup_completed = True
            record.phase = PromotionPhase.CLEANUP_COMPLETE
        else:
            record.cleanup_completed = False
            record.cleanup_error = cleanup_error
        record.updated_at = _utc_now()
        session.add(record)
        session.flush()
        session.refresh(record)
        result = record.model_copy(deep=True)
    return result


async def _rollback_before_commit(
    *,
    promotion_id: int,
    config: AppConfig,
    owner_token: str,
) -> None:
    del owner_token
    engine = create_db_engine(config.database.url)
    with Session(engine) as session:
        record = _require_promotion(session, promotion_id)
        mode = PromotionMode(record.mode)
        source_path = Path(record.source_path)
        final_path = Path(record.final_path)
        staging_path = Path(record.staging_path)
        backup_path = Path(record.backup_path) if record.backup_path is not None else None
        source_stat = FileStatSnapshot.model_validate_json(record.source_stat_json)
        expected_digest = record.validated_output_digest

    try:
        if mode == PromotionMode.KEEP_ORIGINAL:
            if final_path.exists() and expected_digest == calculate_promotion_digest(final_path):
                final_path.unlink()
                fsync_directory(final_path.parent)
        else:
            if backup_path is None or not backup_path.exists():
                raise PromotionRollbackError("Rollback backup is missing.")
            if (
                expected_digest is not None
                and calculate_promotion_digest(source_path) == expected_digest
            ):
                os.replace(backup_path, source_path)
                fsync_directory(source_path.parent)
                if _stat_snapshot(source_path) != source_stat:
                    raise PromotionRollbackError("Rollback did not restore original source stat.")
        _unlink_owned_staging(staging_path)
    finally:
        with Session(engine) as session, session.begin():
            record = _require_promotion(session, promotion_id)
            job = _require_job(session, record.job_id)
            attempt = _require_attempt(session, record.attempt_id)
            record.status = PromotionStatus.ROLLED_BACK
            record.phase = PromotionPhase.ROLLED_BACK
            record.finished_at = _utc_now()
            record.updated_at = _utc_now()
            record.owner_token = None
            record.lease_expires_at = None
            attempt.status = AttemptStatus.INTERRUPTED
            attempt.finished_at = _utc_now()
            transition_job(job, JobStatus.READY_TO_PROMOTE, now=_utc_now())
            job.stage = JobStage.PROMOTE
            job.claimed_by = None
            job.finished_at = None
            job.updated_at = _utc_now()
            session.add(record)
            session.add(attempt)
            session.add(job)


def _commit_verified_promotion(
    session: Session,
    *,
    record: PromotionRecord,
    now: datetime,
) -> None:
    job = _require_job(session, record.job_id)
    attempt = _require_attempt(session, record.attempt_id)
    mode = PromotionMode(record.mode)
    final_path = Path(record.final_path)
    installed_path = Path(record.source_path) if mode != PromotionMode.KEEP_ORIGINAL else final_path
    final_snapshot = create_file_snapshot(installed_path)

    record.status = PromotionStatus.COMPLETED
    record.phase = PromotionPhase.COMMITTED
    record.final_fingerprint = final_snapshot.fs_fingerprint
    record.finished_at = now
    record.updated_at = now
    record.owner_token = None
    record.lease_expires_at = None
    attempt.status = AttemptStatus.COMPLETED
    attempt.finished_at = now
    attempt.output_path = str(installed_path)
    job.latest_promotion_id = record.id
    transition_job(job, JobStatus.PROMOTED, reason=JobOutcomeReason.SUCCESS, now=now)
    job.stage = JobStage.PROMOTE
    job.claimed_by = None
    job.last_error_type = None
    job.last_error_message = None
    job.finished_at = now
    job.updated_at = now
    if mode != PromotionMode.KEEP_ORIGINAL:
        media_file = session.get(MediaFile, job.media_file_id)
        if media_file is None:
            raise PromotionPersistenceError("Promoted job media file no longer exists.")
        media_file.size_bytes = final_snapshot.size_bytes
        media_file.mtime_ns = final_snapshot.mtime_ns
        media_file.device_id = final_snapshot.device_id
        media_file.inode = final_snapshot.inode
        media_file.fs_fingerprint = final_snapshot.fs_fingerprint
        media_file.last_seen_at = now
        media_file.status = MediaFileStatus.PRESENT
        media_file.latest_probe_id = None
        session.add(media_file)
    session.add(record)
    session.add(attempt)
    session.add(job)


def _cleanup_after_success(engine: Engine, promotion_id: int, plan: TranscodePlan) -> str | None:
    errors: list[str] = []
    with Session(engine) as session:
        record = _require_promotion(session, promotion_id)
        mode = PromotionMode(record.mode)
        backup_path = Path(record.backup_path) if record.backup_path is not None else None
        staging_path = Path(record.staging_path)
    if mode == PromotionMode.REPLACE_ATOMIC and backup_path is not None and backup_path.exists():
        try:
            _verify_hardlink_backup(
                source_path=backup_path,
                backup_path=backup_path,
                expected_source=FileStatSnapshot.model_validate_json(record.source_stat_json),
                same_inode=False,
            )
            backup_path.unlink()
            fsync_directory(backup_path.parent)
        except Exception as exc:
            errors.append(f"backup cleanup failed: {exc}")
    for path in (plan.output_path, plan.av1an.video_output_path, staging_path):
        try:
            _delete_known_work_path(path, work_dir=plan.temp_dir)
        except Exception as exc:
            errors.append(f"cleanup failed for {path}: {exc}")
    try:
        _delete_known_work_path(plan.av1an.temp_dir, work_dir=plan.temp_dir, recursive=True)
    except Exception as exc:
        errors.append(f"cleanup failed for {plan.av1an.temp_dir}: {exc}")
    return "; ".join(errors)[:500] if errors else None


def _delete_known_work_path(path: Path, *, work_dir: Path, recursive: bool = False) -> None:
    if not path.exists() and not path.is_symlink():
        return
    resolved_work = work_dir.resolve()
    try:
        path.resolve().relative_to(resolved_work)
    except ValueError as exc:
        raise PromotionFilesystemError(f"Cleanup path is outside work directory: {path}") from exc
    if path.is_symlink():
        raise PromotionFilesystemError(f"Cleanup path is a symlink: {path}")
    if path.is_dir():
        if not recursive:
            raise PromotionFilesystemError(f"Cleanup path is an unexpected directory: {path}")
        shutil.rmtree(path)
        fsync_directory(path.parent)
        return
    path.unlink()
    fsync_directory(path.parent)


def _set_phase(
    engine: Engine,
    promotion_id: int,
    phase: PromotionPhase,
    owner_token: str,
) -> None:
    with Session(engine) as session, session.begin():
        record = _require_promotion(session, promotion_id)
        if record.owner_token != owner_token:
            raise PromotionLeaseError("Promotion lease belongs to another owner.")
        record.phase = phase
        record.updated_at = _utc_now()
        session.add(record)


def _write_journal_from_record(engine: Engine, promotion_id: int) -> None:
    with Session(engine) as session:
        record = _require_promotion(session, promotion_id)
        journal = PromotionJournal(
            promotion_id=_require_id(record),
            operation_id=record.operation_id,
            job_id=record.job_id,
            validation_result_id=record.validation_result_id,
            mode=PromotionMode(record.mode),
            status=PromotionStatus(record.status),
            phase=PromotionPhase(record.phase),
            source_path=Path(record.source_path),
            validated_output_path=Path(record.validated_output_path),
            final_path=Path(record.final_path),
            staging_path=Path(record.staging_path),
            backup_path=Path(record.backup_path) if record.backup_path is not None else None,
            source_fingerprint_before=record.source_fingerprint_before,
            source_stat=FileStatSnapshot.model_validate_json(record.source_stat_json),
            validated_output_fingerprint=record.validated_output_fingerprint,
            validated_output_digest=record.validated_output_digest,
            staging_digest=record.staging_digest,
            final_digest=record.final_digest,
            started_at=record.started_at or record.created_at,
            updated_at=record.updated_at,
        )
        write_promotion_journal(Path(record.journal_path), journal)


def mark_promotion_failed_or_validated(
    session: Session,
    *,
    promotion_id: int,
    error: Exception,
    now: datetime,
) -> None:
    record = _require_promotion(session, promotion_id)
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
    backup_exists = record.backup_path is not None and Path(record.backup_path).exists()
    if not Path(record.final_path).exists() and not backup_exists:
        transition_job(job, JobStatus.READY_TO_PROMOTE, now=now)
        job.finished_at = None
    else:
        transition_job(job, JobStatus.FAILED, reason=JobOutcomeReason.FAILED_PROMOTION, now=now)
        job.finished_at = now
    job.stage = JobStage.PROMOTE
    job.claimed_by = None
    job.last_error_type = error.__class__.__name__
    job.last_error_message = str(error)
    job.updated_at = now
    session.add(record)
    session.add(attempt)
    session.add(job)


def _recoverable_promotion(session: Session, *, job_id: int, now: datetime) -> PromotionRecord:
    record = latest_promotion_record(session, job_id=job_id)
    if record is None:
        raise PromotionRecoveryError("No promotion record exists for this job.")
    if record.status == PromotionStatus.COMPLETED:
        raise PromotionRecoveryError("Promotion is already completed.")
    if (
        record.lease_expires_at is not None
        and record.lease_expires_at > now
        and record.owner_token is not None
    ):
        raise PromotionLeaseError("Promotion lease is still active.")
    return record


def _load_job_plan(job: Job) -> TranscodePlan:
    if job.plan_path is None:
        raise PromotionEligibilityError("Job has no plan artifact.")
    try:
        return load_plan_artifact(Path(job.plan_path))
    except PlanArtifactLoadError as exc:
        raise PromotionEligibilityError(f"Unable to load job plan: {job.plan_path}") from exc


def _require_regular_nonsymlink(path: Path, label: str) -> None:
    _reject_symlink(path, label)
    if not path.exists():
        raise PromotionEligibilityError(f"{label} does not exist: {path}")
    if not path.is_file():
        raise PromotionEligibilityError(f"{label} is not a regular file: {path}")


def _reject_symlink(path: Path, label: str) -> None:
    if path.is_symlink():
        raise PromotionEligibilityError(f"{label} is a symlink: {path}")


def _stat_snapshot(path: Path) -> FileStatSnapshot:
    stat_result = path.stat(follow_symlinks=False)
    return FileStatSnapshot(
        size_bytes=stat_result.st_size,
        mtime_ns=stat_result.st_mtime_ns,
        device_id=stat_result.st_dev,
        inode=stat_result.st_ino,
        mode=stat_result.st_mode,
    )


def _verify_hardlink_backup(
    *,
    source_path: Path,
    backup_path: Path,
    expected_source: FileStatSnapshot,
    same_inode: bool = True,
) -> None:
    source_stat = _stat_snapshot(source_path)
    backup_stat = _stat_snapshot(backup_path)
    if same_inode and (
        source_stat.device_id != backup_stat.device_id or source_stat.inode != backup_stat.inode
    ):
        raise PromotionVerificationError("Rollback backup is not a hard link to the source.")
    if (
        backup_stat.device_id != expected_source.device_id
        or backup_stat.inode != expected_source.inode
        or backup_stat.size_bytes != expected_source.size_bytes
        or backup_stat.mtime_ns != expected_source.mtime_ns
    ):
        raise PromotionVerificationError("Rollback backup does not match the original source.")


def _unlink_owned_staging(staging_path: Path) -> None:
    if staging_path.exists() and not staging_path.is_symlink() and staging_path.is_file():
        staging_path.unlink()
        fsync_directory(staging_path.parent)


def _require_job(session: Session, job_id: int) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise PromotionEligibilityError(f"Job not found: {job_id}")
    return job


def _require_attempt(session: Session, attempt_id: int) -> JobAttempt:
    attempt = session.get(JobAttempt, attempt_id)
    if attempt is None:
        raise PromotionPersistenceError(f"Promotion attempt not found: {attempt_id}")
    return attempt


def _require_promotion(session: Session, promotion_id: int) -> PromotionRecord:
    record = session.get(PromotionRecord, promotion_id)
    if record is None:
        raise PromotionPersistenceError(f"Promotion record not found: {promotion_id}")
    return record


def _require_id(value: object) -> int:
    identifier = getattr(value, "id", None)
    if not isinstance(identifier, int):
        raise PromotionPersistenceError("Expected a persisted row id.")
    return identifier


def _status_phase(value: PromotionPhase | str) -> PromotionPhase:
    return value if isinstance(value, PromotionPhase) else PromotionPhase(value)


def _utc_now() -> datetime:
    return datetime.now(UTC)
