from __future__ import annotations

from avarch.application.retry_policy import (
    RetryAction,
    RetryContext,
    RetryPolicy,
    decide_retry,
)
from avarch.domain.resource_policy import ResourceMode
from avarch.models.execution import ProcessFailureReason


def test_automatic_oom_retries_once_with_native_fallback() -> None:
    decision = decide_retry(
        RetryContext(
            resource_mode=ResourceMode.AUTO,
            failure_reason=ProcessFailureReason.RESOURCE_OOM,
        )
    )

    assert decision.action is RetryAction.RETRY_NATIVE
    assert decision.next_workers == "auto"
    assert decision.next_svt_lp == "native"


def test_repeated_resource_failure_exhausts_retry_budget() -> None:
    decision = decide_retry(
        RetryContext(
            resource_mode=ResourceMode.AUTO,
            failure_reason=ProcessFailureReason.RESOURCE_OOM,
            prior_resource_retries=1,
        )
    )

    assert decision.action is RetryAction.FAIL
    assert decision.reason == "resource_retry_budget_exhausted"


def test_manual_mode_does_not_silently_change_values() -> None:
    decision = decide_retry(
        RetryContext(
            resource_mode=ResourceMode.MANUAL,
            failure_reason=ProcessFailureReason.RESOURCE_OOM,
        )
    )

    assert decision.action is RetryAction.FAIL
    assert decision.reason == "manual_override_not_changed"


def test_non_resource_failure_does_not_retry() -> None:
    decision = decide_retry(
        RetryContext(
            resource_mode=ResourceMode.AUTO,
            failure_reason=ProcessFailureReason.ENCODER_FAILURE,
        ),
        policy=RetryPolicy(max_automatic_resource_retries=3),
    )

    assert decision.action is RetryAction.FAIL
    assert decision.reason == "not_resource_exhaustion"
