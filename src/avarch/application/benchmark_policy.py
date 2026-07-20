from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

from avarch.domain.resource_policy import ResourceIntent, ResourceMode


class BenchmarkDecisionReason(StrEnum):
    ALLOWED = "allowed"
    DISABLED = "disabled"
    MANUAL_MODE = "manual_mode"
    NATIVE_MODE = "native_mode"
    UNKNOWN_DURATION = "unknown_duration"
    SHORT_ENCODE = "short_encode"
    CANCELLED = "cancelled"
    RESOURCE_PRESSURE = "resource_pressure"


@dataclass(frozen=True, slots=True)
class BenchmarkPolicy:
    enabled: bool = True
    max_duration_seconds: float = 120.0
    max_predicted_fraction: float = 0.01
    min_predicted_encode_seconds: float = 300.0

    def __post_init__(self) -> None:
        _validate_non_negative("max_duration_seconds", self.max_duration_seconds)
        _validate_non_negative("max_predicted_fraction", self.max_predicted_fraction)
        _validate_non_negative(
            "min_predicted_encode_seconds",
            self.min_predicted_encode_seconds,
        )


@dataclass(frozen=True, slots=True)
class BenchmarkBudgetDecision:
    should_benchmark: bool
    budget_seconds: float
    reason: BenchmarkDecisionReason


def decide_benchmark_budget(
    *,
    policy: BenchmarkPolicy,
    intent: ResourceIntent,
    predicted_encode_seconds: float | None,
    explicit_request: bool = False,
    cancelled: bool = False,
    resource_pressure: bool = False,
) -> BenchmarkBudgetDecision:
    if cancelled:
        return _skip(BenchmarkDecisionReason.CANCELLED)
    if resource_pressure:
        return _skip(BenchmarkDecisionReason.RESOURCE_PRESSURE)
    if not policy.enabled:
        return _skip(BenchmarkDecisionReason.DISABLED)
    if intent.mode is ResourceMode.MANUAL and not explicit_request:
        return _skip(BenchmarkDecisionReason.MANUAL_MODE)
    if intent.mode is ResourceMode.NATIVE and not explicit_request:
        return _skip(BenchmarkDecisionReason.NATIVE_MODE)
    if predicted_encode_seconds is None or not math.isfinite(predicted_encode_seconds):
        return _skip(BenchmarkDecisionReason.UNKNOWN_DURATION)
    if predicted_encode_seconds <= 0:
        return _skip(BenchmarkDecisionReason.UNKNOWN_DURATION)
    if predicted_encode_seconds < policy.min_predicted_encode_seconds:
        return _skip(BenchmarkDecisionReason.SHORT_ENCODE)

    budget_seconds = min(
        policy.max_duration_seconds,
        predicted_encode_seconds * policy.max_predicted_fraction,
    )
    if budget_seconds <= 0:
        return _skip(BenchmarkDecisionReason.SHORT_ENCODE)
    return BenchmarkBudgetDecision(
        should_benchmark=True,
        budget_seconds=budget_seconds,
        reason=BenchmarkDecisionReason.ALLOWED,
    )


def _skip(reason: BenchmarkDecisionReason) -> BenchmarkBudgetDecision:
    return BenchmarkBudgetDecision(
        should_benchmark=False,
        budget_seconds=0.0,
        reason=reason,
    )


def _validate_non_negative(name: str, value: float) -> None:
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be finite and non-negative")
