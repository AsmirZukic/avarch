from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class JobStatus(StrEnum):
    QUEUED = "queued"
    ENCODING = "encoding"
    ENCODED = "encoded"
    VALIDATING = "validating"
    VALIDATION_FAILED = "validation_failed"
    SIZE_REJECTED = "size_rejected"
    READY_TO_PROMOTE = "ready_to_promote"
    PROMOTING = "promoting"
    CLEANING = "cleaning"
    PROMOTED = "promoted"
    SKIPPED = "skipped"
    FAILED = "failed"
    CANCELLED = "cancelled"

    HELD = "held"

    @classmethod
    def _missing_(cls, value: object) -> JobStatus | None:
        legacy = {
            "pending": cls.QUEUED,
            "running": cls.ENCODING,
            "validated": cls.READY_TO_PROMOTE,
            "completed": cls.PROMOTED,
            "canceled": cls.CANCELLED,
        }
        return legacy.get(value) if isinstance(value, str) else None


class JobOutcomeReason(StrEnum):
    SUCCESS = "success"
    SKIPPED_SIZE_NOT_SMALLER = "skipped_size_not_smaller"
    SKIPPED_MINIMUM_SAVINGS_NOT_MET = "skipped_minimum_savings_not_met"
    FAILED_VALIDATION = "failed_validation"
    FAILED_PROMOTION = "failed_promotion"
    FAILED_ENCODING = "failed_encoding"
    CANCELLED_BY_USER = "cancelled_by_user"


class JobStage(StrEnum):
    PROBE = "probe"
    PLAN = "plan"
    SCENE_DETECT = "scene_detect"
    ENCODE = "encode"
    VALIDATE = "validate"
    PROMOTE = "promote"
    CLEANUP = "cleanup"


class ResourceClass(StrEnum):
    CHEAP = "cheap"
    HEAVY_AV1AN = "heavy_av1an"
    FILE_OP = "file_op"


class AttemptStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELED = "canceled"


class JobEventType(StrEnum):
    STAGE_STARTED = "stage_started"
    STAGE_COMPLETED = "stage_completed"
    SCENE_DETECT_STARTED = "scene_detect_started"
    SCENE_DETECT_COMPLETED = "scene_detect_completed"
    STAGE_FAILED = "stage_failed"
    STAGE_CANCELLED = "stage_cancelled"
    HOLD_REQUESTED = "hold_requested"
    HELD = "held"
    HOLD_RELEASED = "hold_released"
    CANCEL_REQUESTED = "cancel_requested"
    CANCELED = "canceled"
    RETRY_REQUESTED = "retry_requested"
    PRIORITY_CHANGED = "priority_changed"
    QUEUE_CLEARED = "queue_cleared"


class ManualValidationAction(StrEnum):
    REUSE_EXISTING = "reuse_existing"
    RUN = "run"
    RESET_FAILED_AND_RUN = "reset_failed_and_run"
    REJECT_RUNNING = "reject_running"
    REJECT_INELIGIBLE = "reject_ineligible"


class JobTransitionError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class JobTransition:
    status: JobStatus
    outcome_reason: JobOutcomeReason | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CompletedStageTransition:
    status: JobStatus
    stage: JobStage | None
    finished_at: datetime | None
    record_held_event: bool = False


@dataclass(frozen=True, slots=True)
class InterruptedStageTransition:
    status: JobStatus
    record_held_event: bool = False


@dataclass(frozen=True, slots=True)
class RetryTransition:
    status: JobStatus
    stage: JobStage


@dataclass(frozen=True, slots=True)
class ValidationResultTransition:
    status: JobStatus
    stage: JobStage
    finished_at: datetime | None
    last_error_type: str | None = None
    last_error_message: str | None = None


@dataclass(frozen=True, slots=True)
class ManualValidationDecision:
    action: ManualValidationAction


def active_status_for_stage(stage: JobStage | str) -> JobStatus:
    normalized_stage = JobStage(stage)
    if normalized_stage == JobStage.VALIDATE:
        return JobStatus.VALIDATING
    if normalized_stage == JobStage.PROMOTE:
        return JobStatus.PROMOTING
    if normalized_stage == JobStage.CLEANUP:
        return JobStatus.CLEANING
    return JobStatus.ENCODING


def job_can_be_claimed_for_stage(status: JobStatus | str, stage: JobStage | str) -> bool:
    normalized_status = JobStatus(status)
    normalized_stage = JobStage(stage)
    if normalized_status == JobStatus.QUEUED:
        return True
    return normalized_stage == JobStage.VALIDATE and normalized_status in {
        JobStatus.ENCODED,
        JobStatus.VALIDATING,
    }


def job_is_running(status: JobStatus | str) -> bool:
    return JobStatus(status) == JobStatus.ENCODING


def job_is_running_promotion(status: JobStatus | str, stage: JobStage | str) -> bool:
    return job_is_running(status) and JobStage(stage) == JobStage.PROMOTE


def job_is_terminal_history(status: JobStatus | str) -> bool:
    return JobStatus(status) in {JobStatus.PROMOTED, JobStatus.SKIPPED, JobStatus.CANCELLED}


def job_cancel_is_idempotent(status: JobStatus | str) -> bool:
    return JobStatus(status) == JobStatus.CANCELLED


def job_rejects_cancel(status: JobStatus | str) -> bool:
    return JobStatus(status) in {JobStatus.PROMOTED, JobStatus.SKIPPED}


def job_has_passed_validation(status: JobStatus | str, stage: JobStage | str) -> bool:
    return JobStatus(status) == JobStatus.READY_TO_PROMOTE and JobStage(stage) == JobStage.PROMOTE


def job_can_be_held(status: JobStatus | str, stage: JobStage | str) -> bool:
    normalized_status = JobStatus(status)
    if normalized_status == JobStatus.HELD:
        return True
    if job_is_running_promotion(normalized_status, stage):
        return False
    return normalized_status not in {
        JobStatus.FAILED,
        JobStatus.CANCELLED,
        JobStatus.PROMOTED,
        JobStatus.SKIPPED,
        JobStatus.READY_TO_PROMOTE,
    }


def job_can_change_priority(status: JobStatus | str) -> bool:
    return JobStatus(status) in {JobStatus.QUEUED, JobStatus.HELD}


def job_can_retry(status: JobStatus | str) -> bool:
    return JobStatus(status) in {JobStatus.FAILED, JobStatus.CANCELLED}


def job_can_run_manual_validation(status: JobStatus | str, stage: JobStage | str) -> bool:
    normalized_stage = JobStage(stage)
    if normalized_stage != JobStage.VALIDATE:
        return False
    return JobStatus(status) in {JobStatus.QUEUED, JobStatus.FAILED}


def plan_manual_validation(
    status: JobStatus | str,
    stage: JobStage | str,
    *,
    has_passing_validation: bool,
) -> ManualValidationDecision:
    normalized_status = JobStatus(status)
    normalized_stage = JobStage(stage)
    if normalized_status == JobStatus.ENCODING:
        return ManualValidationDecision(ManualValidationAction.REJECT_RUNNING)
    if (
        has_passing_validation
        and normalized_status == JobStatus.READY_TO_PROMOTE
        and normalized_stage == JobStage.PROMOTE
    ):
        return ManualValidationDecision(ManualValidationAction.REUSE_EXISTING)
    if normalized_stage == JobStage.VALIDATE and normalized_status == JobStatus.FAILED:
        return ManualValidationDecision(ManualValidationAction.RESET_FAILED_AND_RUN)
    if job_can_run_manual_validation(normalized_status, normalized_stage):
        return ManualValidationDecision(ManualValidationAction.RUN)
    return ManualValidationDecision(ManualValidationAction.REJECT_INELIGIBLE)


_ALLOWED_TRANSITIONS: Mapping[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset(
        {
            JobStatus.QUEUED,
            JobStatus.ENCODING,
            JobStatus.VALIDATING,
            JobStatus.READY_TO_PROMOTE,
            JobStatus.PROMOTING,
            JobStatus.CLEANING,
            JobStatus.SKIPPED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.HELD,
        }
    ),
    JobStatus.ENCODING: frozenset(
        {
            JobStatus.ENCODING,
            JobStatus.ENCODED,
            JobStatus.QUEUED,
            JobStatus.HELD,
            JobStatus.SKIPPED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.ENCODED: frozenset(
        {
            JobStatus.ENCODED,
            JobStatus.VALIDATING,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.VALIDATING: frozenset(
        {
            JobStatus.VALIDATING,
            JobStatus.QUEUED,
            JobStatus.HELD,
            JobStatus.READY_TO_PROMOTE,
            JobStatus.VALIDATION_FAILED,
            JobStatus.SIZE_REJECTED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.VALIDATION_FAILED: frozenset({JobStatus.VALIDATION_FAILED, JobStatus.QUEUED}),
    JobStatus.SIZE_REJECTED: frozenset({JobStatus.SIZE_REJECTED, JobStatus.QUEUED}),
    JobStatus.READY_TO_PROMOTE: frozenset(
        {
            JobStatus.READY_TO_PROMOTE,
            JobStatus.QUEUED,
            JobStatus.PROMOTING,
            JobStatus.SIZE_REJECTED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.PROMOTING: frozenset(
        {
            JobStatus.PROMOTING,
            JobStatus.PROMOTED,
            JobStatus.READY_TO_PROMOTE,
            JobStatus.FAILED,
        }
    ),
    JobStatus.CLEANING: frozenset(
        {
            JobStatus.CLEANING,
            JobStatus.QUEUED,
            JobStatus.HELD,
            JobStatus.SIZE_REJECTED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.PROMOTED: frozenset({JobStatus.PROMOTED}),
    JobStatus.SKIPPED: frozenset({JobStatus.SKIPPED, JobStatus.QUEUED}),
    JobStatus.FAILED: frozenset(
        {
            JobStatus.FAILED,
            JobStatus.QUEUED,
            JobStatus.VALIDATING,
            JobStatus.READY_TO_PROMOTE,
            JobStatus.CANCELLED,
        }
    ),
    JobStatus.CANCELLED: frozenset({JobStatus.CANCELLED, JobStatus.QUEUED}),
    JobStatus.HELD: frozenset({JobStatus.HELD, JobStatus.QUEUED, JobStatus.CANCELLED}),
    }


def plan_completed_stage_transition(
    current_status: JobStatus | str,
    completed_stage: JobStage | str,
    *,
    next_stage: JobStage | str | None,
    hold_requested: bool,
    now: datetime,
) -> CompletedStageTransition:
    normalized_stage = JobStage(completed_stage)
    if next_stage is None:
        normalized_current = JobStatus(current_status)
        if normalized_current == JobStatus.PROMOTING:
            status = plan_job_transition(normalized_current, JobStatus.PROMOTED, now=now).status
        else:
            status = JobStatus.PROMOTED
        return CompletedStageTransition(
            status=status,
            stage=None,
            finished_at=now,
        )

    normalized_next_stage = JobStage(next_stage)
    if hold_requested:
        transition = plan_job_transition(current_status, JobStatus.HELD, now=now)
        return CompletedStageTransition(
            status=transition.status,
            stage=normalized_next_stage,
            finished_at=None,
            record_held_event=True,
        )

    if normalized_stage == JobStage.ENCODE and normalized_next_stage == JobStage.VALIDATE:
        target_status = JobStatus.ENCODED
    else:
        target_status = JobStatus.QUEUED
    transition = plan_job_transition(current_status, target_status, now=now)
    return CompletedStageTransition(
        status=transition.status,
        stage=normalized_next_stage,
        finished_at=None,
    )


def plan_failed_stage_transition(
    current_status: JobStatus | str,
    *,
    now: datetime,
) -> JobTransition:
    return plan_job_transition(current_status, JobStatus.FAILED, now=now)


def plan_interrupted_stage_transition(
    current_status: JobStatus | str,
    *,
    hold_requested: bool,
    now: datetime,
) -> InterruptedStageTransition:
    target_status = JobStatus.HELD if hold_requested else JobStatus.QUEUED
    transition = plan_job_transition(current_status, target_status, now=now)
    return InterruptedStageTransition(
        status=transition.status,
        record_held_event=hold_requested,
    )


def plan_canceled_transition(current_status: JobStatus | str, *, now: datetime) -> JobTransition:
    return plan_job_transition(
        current_status,
        JobStatus.CANCELLED,
        reason=JobOutcomeReason.CANCELLED_BY_USER,
        now=now,
    )


def plan_skipped_transition(
    current_status: JobStatus | str,
    *,
    reason: JobOutcomeReason | str | None,
    now: datetime,
) -> JobTransition:
    return plan_job_transition(current_status, JobStatus.SKIPPED, reason=reason, now=now)


def plan_retry_transition(next_stage: JobStage | str) -> RetryTransition:
    normalized_stage = JobStage(next_stage)
    return RetryTransition(
        status=JobStatus.READY_TO_PROMOTE
        if normalized_stage == JobStage.PROMOTE
        else JobStatus.QUEUED,
        stage=normalized_stage,
    )


def plan_validation_result_transition(
    current_status: JobStatus | str,
    *,
    passed: bool,
    failed_summary: str,
    now: datetime,
) -> ValidationResultTransition:
    if passed:
        transition = plan_job_transition(current_status, JobStatus.READY_TO_PROMOTE, now=now)
        return ValidationResultTransition(
            status=transition.status,
            stage=JobStage.PROMOTE,
            finished_at=None,
        )
    transition = plan_job_transition(current_status, JobStatus.FAILED, now=now)
    return ValidationResultTransition(
        status=transition.status,
        stage=JobStage.VALIDATE,
        finished_at=now,
        last_error_type="ValidationFailed",
        last_error_message=failed_summary,
    )


def plan_job_transition(
    current_status: JobStatus | str,
    target_status: JobStatus | str,
    reason: JobOutcomeReason | str | None = None,
    *,
    now: datetime | None = None,
) -> JobTransition:
    normalized_current = JobStatus(current_status)
    normalized_target = JobStatus(target_status)
    allowed = _ALLOWED_TRANSITIONS.get(normalized_current, frozenset())
    if normalized_target not in allowed:
        raise JobTransitionError(
            f"Invalid job transition: {normalized_current.value} -> {normalized_target.value}"
        )
    normalized_reason = JobOutcomeReason(reason) if reason is not None else None
    return JobTransition(
        status=normalized_target,
        outcome_reason=normalized_reason,
        updated_at=now,
    )
