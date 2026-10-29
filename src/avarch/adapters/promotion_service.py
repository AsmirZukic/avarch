from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.engine import Engine
from sqlmodel import Session

from avarch.adapters.filesystem.plans import PlanArtifactLoadError, load_plan_artifact
from avarch.adapters.filesystem.promotion import (
    calculate_promotion_digest,
    fsync_directory,
    write_promotion_journal,
)
from avarch.adapters.filesystem.promotion_io import (
    create_original_rollback_link,
    create_promotion_staging_file,
)
from avarch.adapters.filesystem.promotion_io import (
    delete_known_work_path as _delete_known_work_path,
)
from avarch.adapters.filesystem.promotion_io import (
    require_regular_nonsymlink as _require_regular_nonsymlink,
)
from avarch.adapters.filesystem.promotion_io import (
    same_inode as _same_inode,
)
from avarch.adapters.filesystem.promotion_io import (
    stage_validated_output_sync as _stage_validated_output_sync,
)
from avarch.adapters.filesystem.promotion_io import (
    stat_snapshot as _stat_snapshot,
)
from avarch.adapters.filesystem.promotion_io import (
    unlink_owned_staging as _unlink_owned_staging,
)
from avarch.adapters.filesystem.scanner import create_file_snapshot
from avarch.adapters.promotion_types import (
    PromotionConflictError,
    PromotionEligibilityError,
    PromotionError,
    PromotionFilesystemError,
    PromotionLeaseError,
    PromotionPersistenceError,
    PromotionPreflightResult,
    PromotionRecoveryError,
    PromotionResult,
    PromotionRollbackError,
    PromotionVerificationError,
    StagedOutput,
)
from avarch.adapters.replacement_validation import (
    ReplacementValidationError,
    validate_replacement_media,
)
from avarch.adapters.sqlite.db import create_db_engine
from avarch.adapters.sqlite.models import (
    Job,
    PromotionRecord,
    ValidationResult,
)
from avarch.adapters.sqlite.promotion_approvals import (
    PromotionApprovalError,
    require_replacement_approval,
)
from avarch.adapters.sqlite.promotions import (
    PromotedMediaFileSnapshot,
    PromotionActiveLeaseError,
    PromotionClaimData,
    PromotionClaimPersistenceError,
    PromotionLeaseOwnershipError,
    PromotionRecordNotFoundError,
    PromotionRecoveryLookupError,
    claim_recoverable_promotion,
    commit_verified_promotion,
    has_active_promotion_lease,
    has_active_target_lease,
    has_completed_promotion,
    mark_promotion_rolled_back,
    next_promotion_attempt_number,
    persist_promotion_claim,
    renew_promotion_lease,
    require_promotion_job,
    require_promotion_record,
    set_promotion_phase,
    update_promotion_cleanup,
    update_promotion_staged,
    update_promotion_verified,
)
from avarch.adapters.sqlite.promotions import (
    mark_promotion_failed_or_validated as persist_promotion_failure,
)
from avarch.adapters.sqlite.validations import latest_validation
from avarch.config import AppConfig
from avarch.domain.jobs import (
    job_has_passed_validation,
)
from avarch.domain.promotion import (
    PromotionPathConflictError,
    derive_backup_path,
    derive_keep_original_path,
    derive_promotion_journal_path,
    derive_staging_path,
)
from avarch.domain.promotion_recovery import InstallDecision, decide_install
from avarch.models.plan import TranscodePlan
from avarch.models.promotion import (
    FileStatSnapshot,
    PromotionJournal,
    PromotionMode,
    PromotionPhase,
    PromotionStatus,
)
from avarch.serialization import canonical_json

PROMOTION_LEASE_SECONDS = 30.0
PROMOTION_HEARTBEAT_SECONDS = 5.0
PROMOTION_FREE_SPACE_RESERVE_BYTES = 64 * 1024 * 1024


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
        create_staging_file=create_promotion_staging_file,
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
    if not job_has_passed_validation(job.status, job.stage):
        raise PromotionEligibilityError("Job is not validated and ready for promotion.")
    if job.latest_validation_id != validation.id:
        raise PromotionEligibilityError("Validation result is not the latest job validation.")
    if not validation.passed:
        raise PromotionEligibilityError("Validation result did not pass.")
    if validation.job_id != job.id:
        raise PromotionEligibilityError("Validation result belongs to another job.")
    if validation.plan_hash != job.plan_hash or validation.plan_hash != plan.plan_hash:
        raise PromotionEligibilityError("Validation result does not match the job plan.")
    if plan.source_fs_fingerprint != job.source_fs_fingerprint:
        raise PromotionEligibilityError("Plan source does not match the approved job source.")
    if validation.policy_hash != plan.validation.policy_hash:
        raise PromotionEligibilityError("Validation result does not match the validation policy.")
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
    job = _promotion_job(session, job_id)
    try:
        approval = require_replacement_approval(job, mode)
    except PromotionApprovalError as exc:
        raise PromotionEligibilityError(str(exc)) from exc
    if not job_has_passed_validation(job.status, job.stage):
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

    next_attempt_number = next_promotion_attempt_number(session, job=job, job_id=job_id)

    try:
        record = persist_promotion_claim(
            session,
            job=job,
            claim=PromotionClaimData(
                operation_id=operation_id,
                job_id=job_id,
                validation_result_id=preflight.validation_result_id,
                mode=mode,
                source_path=preflight.source_path,
                validated_output_path=preflight.validated_output_path,
                final_path=preflight.final_path,
                staging_path=preflight.staging_path,
                backup_path=preflight.backup_path,
                source_fingerprint=preflight.source_fingerprint,
                source_stat_json=canonical_json(preflight.source_stat),
                validated_output_fingerprint=preflight.validated_output_fingerprint,
                journal_path=derive_promotion_journal_path(plan.runtime.runtime_dir),
                temp_dir=plan.temp_dir,
                owner_token=owner_token,
                attempt_number=next_attempt_number,
                now=now,
                lease_seconds=PROMOTION_LEASE_SECONDS,
            ),
        )
        record.delete_backup_approved = bool(approval and approval.delete_backup)
        session.add(record)
        return record
    except PromotionClaimPersistenceError as exc:
        raise PromotionPersistenceError(str(exc)) from exc


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

    return await _run_claimed_promotion(
        promotion_id=promotion_id,
        config=config,
        owner_token=owner_token,
    )


async def _run_claimed_promotion(
    *,
    promotion_id: int,
    config: AppConfig,
    owner_token: str,
) -> PromotionRecord:
    engine = create_db_engine(config.database.url)
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
    mode: PromotionMode = PromotionMode.KEEP_ORIGINAL,
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
        try:
            record = claim_recoverable_promotion(
                session,
                job_id=job_id,
                owner_token=owner_token,
                now=_utc_now(),
                lease_seconds=PROMOTION_LEASE_SECONDS,
            )
        except PromotionRecoveryLookupError as exc:
            raise PromotionRecoveryError(str(exc)) from exc
        except PromotionActiveLeaseError as exc:
            raise PromotionLeaseError(str(exc)) from exc
        promotion_id = _require_id(record)
    return await _run_claimed_promotion(
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
        record = _promotion_record(session, promotion_id)
        job = _promotion_job(session, record.job_id)
        plan = _load_job_plan(job)
        try:
            require_replacement_approval(job, PromotionMode(record.mode))
        except PromotionApprovalError as exc:
            raise PromotionRollbackError(f"Recovery required: {exc}") from exc
        if (
            plan.source_fs_fingerprint != record.source_fingerprint_before
            or plan.plan_hash != job.plan_hash
            or str(plan.input_path) != record.source_path
            or str(plan.output_path) != record.validated_output_path
        ):
            raise PromotionRollbackError("Recovery required: plan changed during promotion.")
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

    if promotion_phase in {PromotionPhase.COMMITTED, PromotionPhase.CLEANUP_COMPLETE}:
        return _finish_cleanup(engine, promotion_id, plan)

    if promotion_phase == PromotionPhase.ROLLBACK_PENDING:
        await _rollback_before_commit(
            promotion_id=promotion_id,
            config=config,
            owner_token=owner_token,
        )
        with Session(engine) as session:
            return _promotion_record(session, promotion_id).model_copy(deep=True)

    if promotion_phase in {PromotionPhase.PREPARED, PromotionPhase.STAGING}:
        if staging_path.exists() or staging_path.is_symlink():
            # A pre-staged crash has no persisted inode evidence. Never guess.
            raise PromotionRollbackError("Recovery required: unverified partial staging file.")
        _set_promotion_phase(engine, promotion_id, PromotionPhase.STAGING, owner_token)
        _write_journal_from_record(engine, promotion_id)
        staged = await stage_validated_output(
            source_output=output_path,
            staging_path=staging_path,
            expected_fingerprint=validated_output_fingerprint,
            expected_mode=expected_mode,
            heartbeat=heartbeat,
        )
        with Session(engine) as session, session.begin():
            update_promotion_staged(
                session,
                promotion_id=promotion_id,
                source_digest=staged.source_digest,
                staging_digest=staged.staging_digest,
                now=_utc_now(),
            )
            record = _promotion_record(session, promotion_id)
            record.staging_stat_json = canonical_json(_stat_snapshot(staging_path))
            session.add(record)
        promotion_phase = PromotionPhase.STAGED
        _write_journal_from_record(engine, promotion_id)

    with Session(engine) as session:
        record = _promotion_record(session, promotion_id)
        if record.staging_stat_json is None:
            raise PromotionRollbackError("Recovery required: no persisted staging ownership.")
        installed_stat = FileStatSnapshot.model_validate_json(record.staging_stat_json)
        expected_digest = record.staging_digest

    install_path = final_path if promotion_mode == PromotionMode.KEEP_ORIGINAL else source_path
    decision = decide_install(
        phase=promotion_phase.value,
        installed_owned=_same_inode(install_path, installed_stat),
        staged_owned=_same_inode(staging_path, installed_stat),
        source_unchanged=(source_path.exists() and _stat_snapshot(source_path) == source_stat),
        destination_exists=install_path.exists() or install_path.is_symlink(),
        keep_original=promotion_mode == PromotionMode.KEEP_ORIGINAL,
    )
    if decision == InstallDecision.REQUIRE_RECOVERY:
        raise PromotionRollbackError("Recovery required: installation ownership is ambiguous.")

    if decision == InstallDecision.INSTALL:
        staged_digest = await asyncio.to_thread(
            calculate_promotion_digest,
            staging_path,
            heartbeat=heartbeat.refresh,
        )
        if expected_digest is None or staged_digest != expected_digest:
            raise PromotionVerificationError("Staged output changed before installation.")
        if backup_path is not None:
            if record.replacement_evidence_json is None:
                await _validate_replacement_before_install(
                    engine=engine,
                    promotion_id=promotion_id,
                    plan=plan,
                    staging_path=staging_path,
                    heartbeat=heartbeat,
                )
            if not backup_path.exists():
                _set_promotion_phase(
                    engine, promotion_id, PromotionPhase.BACKUP_PENDING, owner_token
                )
                _write_journal_from_record(engine, promotion_id)
                create_original_rollback_link(
                    source_path=source_path,
                    backup_path=backup_path,
                    expected_source=source_stat,
                )
            with Session(engine) as session:
                record = _promotion_record(session, promotion_id)
                await asyncio.to_thread(_verify_original_backup, record, heartbeat=heartbeat)
            _set_promotion_phase(engine, promotion_id, PromotionPhase.BACKUP_CREATED, owner_token)
            _write_journal_from_record(engine, promotion_id)

        heartbeat.refresh(force=True)
        if _stat_snapshot(source_path) != source_stat:
            raise PromotionVerificationError("Source changed immediately before installation.")
        _set_promotion_phase(engine, promotion_id, PromotionPhase.INSTALL_PENDING, owner_token)
        _write_journal_from_record(engine, promotion_id)
        if promotion_mode == PromotionMode.KEEP_ORIGINAL:
            try:
                os.link(staging_path, install_path, follow_symlinks=False)
            except FileExistsError as exc:
                raise PromotionConflictError(f"Final path already exists: {install_path}") from exc
            fsync_directory(install_path.parent)
            staging_path.unlink()
        else:
            os.replace(staging_path, install_path)
        fsync_directory(install_path.parent)
    _set_promotion_phase(engine, promotion_id, PromotionPhase.FINAL_INSTALLED, owner_token)
    _write_journal_from_record(engine, promotion_id)

    final_digest = await asyncio.to_thread(
        calculate_promotion_digest,
        install_path,
        heartbeat=heartbeat.refresh,
    )
    with Session(engine) as session:
        record = _promotion_record(session, promotion_id)
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
        try:
            update_promotion_verified(
                session,
                promotion_id=promotion_id,
                final_digest=final_digest,
                final_fingerprint=final_snapshot.fs_fingerprint,
                now=_utc_now(),
            )
        except PromotionRecordNotFoundError as exc:
            raise PromotionPersistenceError(str(exc)) from exc
    _write_journal_from_record(engine, promotion_id)

    with Session(engine) as session, session.begin():
        record = _promotion_record(session, promotion_id)
        _commit_verified_promotion(session, record=record, now=_utc_now())
        promotion_id = _require_id(record)

    return _finish_cleanup(engine, promotion_id, plan)


def _finish_cleanup(engine: Engine, promotion_id: int, plan: TranscodePlan) -> PromotionRecord:
    cleanup_error = _cleanup_after_success(engine, promotion_id, plan)
    with Session(engine) as session, session.begin():
        try:
            result = update_promotion_cleanup(
                session,
                promotion_id=promotion_id,
                cleanup_error=cleanup_error,
                now=_utc_now(),
            )
        except PromotionRecordNotFoundError as exc:
            raise PromotionPersistenceError(str(exc)) from exc
    return result


async def _validate_replacement_before_install(
    *,
    engine: Engine,
    promotion_id: int,
    plan: TranscodePlan,
    staging_path: Path,
    heartbeat: PromotionHeartbeat,
) -> None:
    task = asyncio.create_task(
        asyncio.to_thread(
            validate_replacement_media,
            plan.input_path,
            staging_path,
            duration_seconds=plan.validation.source_duration_seconds,
            source_video_index=plan.video.source_stream_index,
            source_audio_index=plan.audio.source_stream_index if plan.audio is not None else None,
        )
    )
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=PROMOTION_HEARTBEAT_SECONDS)
            heartbeat.refresh(force=True)
        evidence = task.result()
    except ReplacementValidationError as exc:
        raise PromotionVerificationError(str(exc)) from exc
    finally:
        # Do not leave a decoder using files after the promotion has returned.
        if not task.done():
            await task
    if create_file_snapshot(plan.input_path).fs_fingerprint != plan.source_fs_fingerprint:
        raise PromotionVerificationError("Source changed during complete decode.")
    staged_digest = await asyncio.to_thread(
        calculate_promotion_digest,
        staging_path,
        heartbeat=heartbeat.refresh,
    )
    original_digest = await asyncio.to_thread(
        calculate_promotion_digest,
        plan.input_path,
        heartbeat=heartbeat.refresh,
    )
    if create_file_snapshot(plan.input_path).fs_fingerprint != plan.source_fs_fingerprint:
        raise PromotionVerificationError("Source changed while hashing replacement evidence.")
    with Session(engine) as session, session.begin():
        record = _promotion_record(session, promotion_id)
        if staged_digest != record.staging_digest:
            raise PromotionVerificationError("Staging changed during complete decode.")
        record.original_digest = original_digest
        record.replacement_evidence_json = canonical_json(evidence)
        session.add(record)


async def _rollback_before_commit(
    *,
    promotion_id: int,
    config: AppConfig,
    owner_token: str,
) -> None:
    engine = create_db_engine(config.database.url)
    with Session(engine) as session:
        record = _promotion_record(session, promotion_id)
        mode = PromotionMode(record.mode)
        source_path = Path(record.source_path)
        final_path = Path(record.final_path)
        staging_path = Path(record.staging_path)
        backup_path = Path(record.backup_path) if record.backup_path is not None else None
        source_stat = FileStatSnapshot.model_validate_json(record.source_stat_json)
        if record.staging_stat_json is None:
            raise PromotionRollbackError("Recovery required: missing installed ownership evidence.")
        installed_stat = FileStatSnapshot.model_validate_json(record.staging_stat_json)
        if record.owner_token != owner_token:
            raise PromotionLeaseError("Promotion lease belongs to another owner.")

    installed_path = final_path if mode == PromotionMode.KEEP_ORIGINAL else source_path
    _set_promotion_phase(engine, promotion_id, PromotionPhase.ROLLBACK_PENDING, owner_token)
    _write_journal_from_record(engine, promotion_id)
    original_already_restored = (
        mode != PromotionMode.KEEP_ORIGINAL
        and source_path.exists()
        and _stat_snapshot(source_path) == source_stat
        and record.original_digest is not None
        and calculate_promotion_digest(source_path) == record.original_digest
    )
    if not original_already_restored and not _same_inode(installed_path, installed_stat):
        raise PromotionRollbackError("Recovery required: installed path ownership changed.")
    if mode == PromotionMode.KEEP_ORIGINAL:
        final_path.unlink()
        fsync_directory(final_path.parent)
    elif not original_already_restored:
        _verify_original_backup(record)
        assert backup_path is not None
        try:
            os.replace(backup_path, source_path)
            fsync_directory(source_path.parent)
        except OSError as exc:
            raise PromotionRollbackError("Recovery required: restoration failed.") from exc
        if (
            _stat_snapshot(source_path) != source_stat
            or calculate_promotion_digest(source_path) != record.original_digest
        ):
            raise PromotionRollbackError("Recovery required: original was not restored.")
    _unlink_owned_staging(staging_path, installed_stat)
    with Session(engine) as session, session.begin():
        try:
            mark_promotion_rolled_back(session, promotion_id=promotion_id, now=_utc_now())
        except PromotionRecordNotFoundError as exc:
            raise PromotionPersistenceError(str(exc)) from exc


def _verify_original_backup(
    record: PromotionRecord,
    *,
    heartbeat: PromotionHeartbeat | None = None,
) -> None:
    path = Path(record.backup_path) if record.backup_path is not None else None
    expected = FileStatSnapshot.model_validate_json(record.source_stat_json)
    if (
        path is None
        or path.is_symlink()
        or not path.is_file()
        or _stat_snapshot(path) != expected
        or record.original_digest is None
        or calculate_promotion_digest(
            path,
            heartbeat=heartbeat.refresh if heartbeat is not None else None,
        )
        != record.original_digest
    ):
        raise PromotionRollbackError("Recovery required: original backup is missing or changed.")


def _commit_verified_promotion(
    session: Session,
    *,
    record: PromotionRecord,
    now: datetime,
) -> None:
    mode = PromotionMode(record.mode)
    final_path = Path(record.final_path)
    installed_path = Path(record.source_path) if mode != PromotionMode.KEEP_ORIGINAL else final_path
    final_snapshot = create_file_snapshot(installed_path)
    media_snapshot = None
    if mode != PromotionMode.KEEP_ORIGINAL:
        media_snapshot = PromotedMediaFileSnapshot(
            size_bytes=final_snapshot.size_bytes,
            mtime_ns=final_snapshot.mtime_ns,
            device_id=final_snapshot.device_id,
            inode=final_snapshot.inode,
            fs_fingerprint=final_snapshot.fs_fingerprint,
        )
    try:
        commit_verified_promotion(
            session,
            record=record,
            installed_path=installed_path,
            final_fingerprint=final_snapshot.fs_fingerprint,
            media_snapshot=media_snapshot,
            now=now,
        )
    except PromotionRecordNotFoundError as exc:
        raise PromotionPersistenceError(str(exc)) from exc


def _cleanup_after_success(engine: Engine, promotion_id: int, plan: TranscodePlan) -> str | None:
    errors: list[str] = []
    with Session(engine) as session:
        record = _promotion_record(session, promotion_id)
        mode = PromotionMode(record.mode)
        backup_path = Path(record.backup_path) if record.backup_path is not None else None
        staging_path = Path(record.staging_path)
    if (
        mode == PromotionMode.REPLACE_ATOMIC
        and record.delete_backup_approved
        and backup_path is not None
        and backup_path.exists()
    ):
        try:
            _verify_original_backup(record)
            installed = Path(record.final_path)
            if (
                record.final_digest is None
                or installed.is_symlink()
                or calculate_promotion_digest(installed) != record.final_digest
            ):
                raise PromotionVerificationError(
                    "Installed output changed; retaining original backup."
                )
            backup_path.unlink()
            fsync_directory(backup_path.parent)
        except Exception as exc:
            errors.append(f"backup cleanup failed: {exc}")
    if record.staging_stat_json is not None:
        try:
            _unlink_owned_staging(
                staging_path,
                FileStatSnapshot.model_validate_json(record.staging_stat_json),
            )
        except PromotionError as exc:
            errors.append(f"staging cleanup failed: {exc}")
    for path in (plan.output_path, plan.video_output_path):
        try:
            _delete_known_work_path(path, work_dir=plan.temp_dir)
        except Exception as exc:
            errors.append(f"cleanup failed for {path}: {exc}")
    try:
        _delete_known_work_path(plan.temp_dir, work_dir=plan.temp_dir, recursive=True)
    except Exception as exc:
        errors.append(f"cleanup failed for {plan.temp_dir}: {exc}")
    return "; ".join(errors)[:500] if errors else None


def _set_promotion_phase(
    engine: Engine,
    promotion_id: int,
    phase: PromotionPhase,
    owner_token: str,
) -> None:
    try:
        set_promotion_phase(
            engine,
            promotion_id=promotion_id,
            phase=phase,
            owner_token=owner_token,
            now=_utc_now(),
        )
    except PromotionRecordNotFoundError as exc:
        raise PromotionPersistenceError(str(exc)) from exc
    except PromotionLeaseOwnershipError as exc:
        raise PromotionLeaseError(str(exc)) from exc


def _write_journal_from_record(engine: Engine, promotion_id: int) -> None:
    with Session(engine) as session:
        record = _promotion_record(session, promotion_id)
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
            staging_stat=(
                FileStatSnapshot.model_validate_json(record.staging_stat_json)
                if record.staging_stat_json is not None
                else None
            ),
            original_digest=record.original_digest,
            replacement_evidence_json=record.replacement_evidence_json,
            delete_backup_approved=record.delete_backup_approved,
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
    record = _promotion_record(session, promotion_id)
    if record.phase in {PromotionPhase.COMMITTED, PromotionPhase.CLEANUP_COMPLETE}:
        record.cleanup_error = str(error)
        session.add(record)
        return
    backup_exists = record.backup_path is not None and Path(record.backup_path).exists()
    try:
        persist_promotion_failure(
            session,
            promotion_id=promotion_id,
            error=error,
            can_return_to_promote=(
                record.status == PromotionStatus.ROLLED_BACK
                or (not Path(record.final_path).exists() and not backup_exists)
            ),
            now=now,
        )
        if isinstance(error, PromotionRollbackError):
            record.status = PromotionStatus.RECOVERY_REQUIRED
            record.phase = PromotionPhase.RECOVERY_REQUIRED
            session.add(record)
    except PromotionRecordNotFoundError as exc:
        raise PromotionPersistenceError(str(exc)) from exc


def _load_job_plan(job: Job) -> TranscodePlan:
    if job.plan_path is None:
        raise PromotionEligibilityError("Job has no plan artifact.")
    try:
        return load_plan_artifact(Path(job.plan_path))
    except PlanArtifactLoadError as exc:
        raise PromotionEligibilityError(f"Unable to load job plan: {job.plan_path}") from exc


def _promotion_job(session: Session, job_id: int) -> Job:
    try:
        return require_promotion_job(session, job_id=job_id)
    except PromotionRecordNotFoundError as exc:
        raise PromotionEligibilityError(str(exc)) from exc


def _promotion_record(session: Session, promotion_id: int) -> PromotionRecord:
    try:
        return require_promotion_record(session, promotion_id=promotion_id)
    except PromotionRecordNotFoundError as exc:
        raise PromotionPersistenceError(str(exc)) from exc


def _require_id(value: object) -> int:
    identifier = getattr(value, "id", None)
    if not isinstance(identifier, int):
        raise PromotionPersistenceError("Expected a persisted row id.")
    return identifier


def _status_phase(value: PromotionPhase | str) -> PromotionPhase:
    return value if isinstance(value, PromotionPhase) else PromotionPhase(value)


def _utc_now() -> datetime:
    return datetime.now(UTC)
