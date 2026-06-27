from __future__ import annotations

from datetime import UTC, datetime

import pytest

from avarch.domain.jobs import (
    JobOutcomeReason,
    JobStage,
    JobStatus,
    JobTransitionError,
    ManualValidationAction,
    active_status_for_stage,
    job_can_be_claimed_for_stage,
    job_can_be_held,
    job_can_change_priority,
    job_can_retry,
    job_can_run_manual_validation,
    job_cancel_is_idempotent,
    job_has_passed_validation,
    job_is_running,
    job_is_running_promotion,
    job_is_terminal_history,
    job_rejects_cancel,
    plan_canceled_transition,
    plan_completed_stage_transition,
    plan_interrupted_stage_transition,
    plan_job_transition,
    plan_manual_validation,
    plan_retry_transition,
    plan_skipped_transition,
    plan_validation_result_transition,
)


def test_plan_job_transition_returns_transition_value() -> None:
    now = datetime.now(UTC)

    transition = plan_job_transition(
        JobStatus.VALIDATING,
        JobStatus.SIZE_REJECTED,
        reason=JobOutcomeReason.SKIPPED_SIZE_NOT_SMALLER,
        now=now,
    )

    assert transition.status == JobStatus.SIZE_REJECTED
    assert transition.outcome_reason == JobOutcomeReason.SKIPPED_SIZE_NOT_SMALLER
    assert transition.updated_at == now


def test_plan_job_transition_rejects_impossible_transition() -> None:
    with pytest.raises(JobTransitionError):
        plan_job_transition(JobStatus.ENCODING, JobStatus.PROMOTED)


def test_plan_job_transition_accepts_legacy_status_names() -> None:
    transition = plan_job_transition("pending", "running")

    assert transition.status == JobStatus.ENCODING


def test_plan_job_transition_normalizes_reason_value() -> None:
    transition = plan_job_transition(
        JobStatus.VALIDATING,
        JobStatus.VALIDATION_FAILED,
        reason="failed_validation",
    )

    assert transition.outcome_reason == JobOutcomeReason.FAILED_VALIDATION


@pytest.mark.parametrize(
    "stage,expected_status",
    [
        (JobStage.PROBE, JobStatus.ENCODING),
        (JobStage.PLAN, JobStatus.ENCODING),
        (JobStage.ENCODE, JobStatus.ENCODING),
        (JobStage.VALIDATE, JobStatus.VALIDATING),
        (JobStage.PROMOTE, JobStatus.PROMOTING),
        (JobStage.CLEANUP, JobStatus.CLEANING),
    ],
)
def test_active_status_for_stage(stage: JobStage, expected_status: JobStatus) -> None:
    assert active_status_for_stage(stage) == expected_status


@pytest.mark.parametrize(
    "status,stage,expected",
    [
        (JobStatus.QUEUED, JobStage.PROBE, True),
        (JobStatus.ENCODED, JobStage.VALIDATE, True),
        (JobStatus.VALIDATING, JobStage.VALIDATE, True),
        (JobStatus.ENCODED, JobStage.ENCODE, False),
        (JobStatus.FAILED, JobStage.VALIDATE, False),
    ],
)
def test_job_can_be_claimed_for_stage(
    status: JobStatus,
    stage: JobStage,
    expected: bool,
) -> None:
    assert job_can_be_claimed_for_stage(status, stage) is expected


def test_job_status_classification_helpers() -> None:
    assert job_is_running(JobStatus.ENCODING) is True
    assert job_is_running(JobStatus.QUEUED) is False
    assert job_is_running_promotion(JobStatus.ENCODING, JobStage.PROMOTE) is True
    assert job_is_running_promotion(JobStatus.ENCODING, JobStage.ENCODE) is False
    assert job_is_terminal_history(JobStatus.PROMOTED) is True
    assert job_is_terminal_history(JobStatus.FAILED) is False
    assert job_cancel_is_idempotent(JobStatus.CANCELLED) is True
    assert job_rejects_cancel(JobStatus.PROMOTED) is True
    assert job_rejects_cancel(JobStatus.QUEUED) is False


@pytest.mark.parametrize(
    "status,stage,expected",
    [
        (JobStatus.QUEUED, JobStage.ENCODE, True),
        (JobStatus.ENCODING, JobStage.ENCODE, True),
        (JobStatus.ENCODING, JobStage.PROMOTE, False),
        (JobStatus.FAILED, JobStage.ENCODE, False),
        (JobStatus.READY_TO_PROMOTE, JobStage.PROMOTE, False),
        (JobStatus.HELD, JobStage.ENCODE, True),
    ],
)
def test_job_can_be_held(
    status: JobStatus,
    stage: JobStage,
    expected: bool,
) -> None:
    assert job_can_be_held(status, stage) is expected


@pytest.mark.parametrize(
    "status,expected",
    [
        (JobStatus.QUEUED, True),
        (JobStatus.HELD, True),
        (JobStatus.ENCODING, False),
        (JobStatus.FAILED, False),
    ],
)
def test_job_can_change_priority(status: JobStatus, expected: bool) -> None:
    assert job_can_change_priority(status) is expected


@pytest.mark.parametrize(
    "status,expected",
    [
        (JobStatus.FAILED, True),
        (JobStatus.CANCELLED, True),
        (JobStatus.QUEUED, False),
        (JobStatus.PROMOTED, False),
    ],
)
def test_job_can_retry(status: JobStatus, expected: bool) -> None:
    assert job_can_retry(status) is expected


@pytest.mark.parametrize(
    "status,stage,expected",
    [
        (JobStatus.READY_TO_PROMOTE, JobStage.PROMOTE, True),
        (JobStatus.QUEUED, JobStage.VALIDATE, False),
        (JobStatus.READY_TO_PROMOTE, JobStage.VALIDATE, False),
    ],
)
def test_job_has_passed_validation(
    status: JobStatus,
    stage: JobStage,
    expected: bool,
) -> None:
    assert job_has_passed_validation(status, stage) is expected


@pytest.mark.parametrize(
    "status,stage,expected",
    [
        (JobStatus.QUEUED, JobStage.VALIDATE, True),
        (JobStatus.FAILED, JobStage.VALIDATE, True),
        (JobStatus.ENCODED, JobStage.VALIDATE, False),
        (JobStatus.QUEUED, JobStage.ENCODE, False),
    ],
)
def test_job_can_run_manual_validation(
    status: JobStatus,
    stage: JobStage,
    expected: bool,
) -> None:
    assert job_can_run_manual_validation(status, stage) is expected


@pytest.mark.parametrize(
    "status,stage,has_passing_validation,expected",
    [
        (
            JobStatus.READY_TO_PROMOTE,
            JobStage.PROMOTE,
            True,
            ManualValidationAction.REUSE_EXISTING,
        ),
        (JobStatus.QUEUED, JobStage.VALIDATE, False, ManualValidationAction.RUN),
        (
            JobStatus.FAILED,
            JobStage.VALIDATE,
            False,
            ManualValidationAction.RESET_FAILED_AND_RUN,
        ),
        (JobStatus.ENCODING, JobStage.VALIDATE, False, ManualValidationAction.REJECT_RUNNING),
        (
            JobStatus.READY_TO_PROMOTE,
            JobStage.PROMOTE,
            False,
            ManualValidationAction.REJECT_INELIGIBLE,
        ),
    ],
)
def test_plan_manual_validation(
    status: JobStatus,
    stage: JobStage,
    has_passing_validation: bool,
    expected: ManualValidationAction,
) -> None:
    decision = plan_manual_validation(
        status,
        stage,
        has_passing_validation=has_passing_validation,
    )

    assert decision.action == expected


def test_completed_encode_waits_for_validation() -> None:
    now = datetime.now(UTC)

    transition = plan_completed_stage_transition(
        JobStatus.ENCODING,
        JobStage.ENCODE,
        next_stage=JobStage.VALIDATE,
        hold_requested=False,
        now=now,
    )

    assert transition.status == JobStatus.ENCODED
    assert transition.stage == JobStage.VALIDATE
    assert transition.finished_at is None
    assert transition.record_held_event is False


def test_completed_stage_respects_pending_hold_request() -> None:
    transition = plan_completed_stage_transition(
        JobStatus.ENCODING,
        JobStage.PLAN,
        next_stage=JobStage.ENCODE,
        hold_requested=True,
        now=datetime.now(UTC),
    )

    assert transition.status == JobStatus.HELD
    assert transition.stage == JobStage.ENCODE
    assert transition.record_held_event is True


def test_interrupted_stage_returns_to_claimable_state() -> None:
    transition = plan_interrupted_stage_transition(
        JobStatus.ENCODING,
        hold_requested=False,
        now=datetime.now(UTC),
    )

    assert transition.status == JobStatus.QUEUED
    assert transition.record_held_event is False


def test_retry_to_promote_restores_ready_to_promote_state() -> None:
    transition = plan_retry_transition(JobStage.PROMOTE)

    assert transition.status == JobStatus.READY_TO_PROMOTE
    assert transition.stage == JobStage.PROMOTE


def test_validation_success_moves_to_promotion() -> None:
    transition = plan_validation_result_transition(
        JobStatus.VALIDATING,
        passed=True,
        failed_summary="",
        now=datetime.now(UTC),
    )

    assert transition.status == JobStatus.READY_TO_PROMOTE
    assert transition.stage == JobStage.PROMOTE
    assert transition.finished_at is None


def test_validation_failure_records_terminal_failure() -> None:
    now = datetime.now(UTC)

    transition = plan_validation_result_transition(
        JobStatus.VALIDATING,
        passed=False,
        failed_summary="duration_close failed",
        now=now,
    )

    assert transition.status == JobStatus.FAILED
    assert transition.stage == JobStage.VALIDATE
    assert transition.finished_at == now
    assert transition.last_error_type == "ValidationFailed"
    assert transition.last_error_message == "duration_close failed"


def test_cancel_and_skip_are_explicit_transitions() -> None:
    now = datetime.now(UTC)

    canceled = plan_canceled_transition(JobStatus.QUEUED, now=now)
    skipped = plan_skipped_transition(JobStatus.ENCODING, reason=None, now=now)

    assert canceled.status == JobStatus.CANCELLED
    assert canceled.outcome_reason == JobOutcomeReason.CANCELLED_BY_USER
    assert skipped.status == JobStatus.SKIPPED
