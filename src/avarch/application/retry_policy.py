from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from avarch.domain.resource_policy import ResourceMode
from avarch.models.execution import ProcessFailureReason


class RetryAction(StrEnum):
    FAIL = "fail"
    RETRY_NATIVE = "retry_native"


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_automatic_resource_retries: int = 1
    allow_manual_resource_fallback: bool = False


@dataclass(frozen=True, slots=True)
class RetryContext:
    resource_mode: ResourceMode
    failure_reason: ProcessFailureReason | None
    prior_resource_retries: int = 0


@dataclass(frozen=True, slots=True)
class RetryDecision:
    action: RetryAction
    reason: str
    next_workers: int | str | None = None
    next_svt_lp: int | str | None = None


DEFAULT_RETRY_POLICY = RetryPolicy()


def decide_retry(
    context: RetryContext,
    *,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
) -> RetryDecision:
    if context.failure_reason is not ProcessFailureReason.RESOURCE_OOM:
        return RetryDecision(action=RetryAction.FAIL, reason="not_resource_exhaustion")
    if context.resource_mode is ResourceMode.MANUAL and not policy.allow_manual_resource_fallback:
        return RetryDecision(action=RetryAction.FAIL, reason="manual_override_not_changed")
    if context.prior_resource_retries >= policy.max_automatic_resource_retries:
        return RetryDecision(action=RetryAction.FAIL, reason="resource_retry_budget_exhausted")
    return RetryDecision(
        action=RetryAction.RETRY_NATIVE,
        reason="resource_exhaustion_native_fallback",
        next_workers="auto",
        next_svt_lp="native",
    )
