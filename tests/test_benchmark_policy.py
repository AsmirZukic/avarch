from __future__ import annotations

import pytest

from avarch.application.benchmark_policy import (
    BenchmarkDecisionReason,
    BenchmarkPolicy,
    decide_benchmark_budget,
)
from avarch.domain.resource_policy import parse_resource_intent


def test_benchmark_budget_is_capped_by_duration_and_predicted_fraction() -> None:
    decision = decide_benchmark_budget(
        policy=BenchmarkPolicy(max_duration_seconds=120, max_predicted_fraction=0.01),
        intent=parse_resource_intent(workers="auto"),
        predicted_encode_seconds=20_000,
    )
    fraction_limited = decide_benchmark_budget(
        policy=BenchmarkPolicy(max_duration_seconds=120, max_predicted_fraction=0.01),
        intent=parse_resource_intent(workers="auto"),
        predicted_encode_seconds=6_000,
    )

    assert decision.should_benchmark is True
    assert decision.budget_seconds == 120
    assert fraction_limited.should_benchmark is True
    assert fraction_limited.budget_seconds == 60


def test_short_or_unknown_encodes_skip_calibration() -> None:
    policy = BenchmarkPolicy(min_predicted_encode_seconds=300)

    short = decide_benchmark_budget(
        policy=policy,
        intent=parse_resource_intent(workers="auto"),
        predicted_encode_seconds=299,
    )
    unknown = decide_benchmark_budget(
        policy=policy,
        intent=parse_resource_intent(workers="auto"),
        predicted_encode_seconds=None,
    )

    assert short.should_benchmark is False
    assert short.reason == BenchmarkDecisionReason.SHORT_ENCODE
    assert unknown.should_benchmark is False
    assert unknown.reason == BenchmarkDecisionReason.UNKNOWN_DURATION


def test_manual_and_native_modes_skip_without_explicit_request() -> None:
    manual = decide_benchmark_budget(
        policy=BenchmarkPolicy(),
        intent=parse_resource_intent(mode="manual", workers=2, svt_lp=4),
        predicted_encode_seconds=10_000,
    )
    native = decide_benchmark_budget(
        policy=BenchmarkPolicy(),
        intent=parse_resource_intent(mode="native", workers="auto"),
        predicted_encode_seconds=10_000,
    )

    assert manual.reason == BenchmarkDecisionReason.MANUAL_MODE
    assert native.reason == BenchmarkDecisionReason.NATIVE_MODE


def test_explicit_request_can_benchmark_manual_or_native_modes() -> None:
    manual = decide_benchmark_budget(
        policy=BenchmarkPolicy(),
        intent=parse_resource_intent(mode="manual", workers=2, svt_lp=4),
        predicted_encode_seconds=10_000,
        explicit_request=True,
    )
    native = decide_benchmark_budget(
        policy=BenchmarkPolicy(),
        intent=parse_resource_intent(mode="native", workers="auto"),
        predicted_encode_seconds=10_000,
        explicit_request=True,
    )

    assert manual.should_benchmark is True
    assert native.should_benchmark is True


def test_cancellation_and_resource_pressure_terminate_calibration() -> None:
    cancelled = decide_benchmark_budget(
        policy=BenchmarkPolicy(),
        intent=parse_resource_intent(workers="auto"),
        predicted_encode_seconds=10_000,
        explicit_request=True,
        cancelled=True,
    )
    pressured = decide_benchmark_budget(
        policy=BenchmarkPolicy(),
        intent=parse_resource_intent(workers="auto"),
        predicted_encode_seconds=10_000,
        explicit_request=True,
        resource_pressure=True,
    )

    assert cancelled.should_benchmark is False
    assert cancelled.reason == BenchmarkDecisionReason.CANCELLED
    assert pressured.should_benchmark is False
    assert pressured.reason == BenchmarkDecisionReason.RESOURCE_PRESSURE


def test_invalid_benchmark_policy_values_are_rejected() -> None:
    with pytest.raises(ValueError):
        BenchmarkPolicy(max_duration_seconds=-1)
    with pytest.raises(ValueError):
        BenchmarkPolicy(max_predicted_fraction=float("nan"))
