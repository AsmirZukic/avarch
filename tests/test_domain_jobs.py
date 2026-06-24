from __future__ import annotations

from datetime import UTC, datetime

import pytest

from avarch.domain.jobs import (
    JobOutcomeReason,
    JobStatus,
    JobTransitionError,
    plan_job_transition,
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