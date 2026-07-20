from __future__ import annotations

from dataclasses import dataclass

from avarch.application.benchmark_policy import (
    BenchmarkBudgetDecision,
    BenchmarkPolicy,
    decide_benchmark_budget,
)
from avarch.config import PerformanceSettings
from avarch.domain.resource_policy import ResourceIntent


@dataclass(frozen=True, slots=True)
class CalibrationPlan:
    decision: BenchmarkBudgetDecision


def benchmark_policy_from_config(settings: PerformanceSettings) -> BenchmarkPolicy:
    return BenchmarkPolicy(
        enabled=settings.calibration_enabled,
        max_duration_seconds=settings.calibration_max_seconds,
        max_predicted_fraction=settings.calibration_max_predicted_fraction,
        min_predicted_encode_seconds=settings.calibration_min_predicted_seconds,
    )


def plan_calibration(
    *,
    settings: PerformanceSettings,
    intent: ResourceIntent,
    predicted_encode_seconds: float | None,
    explicit_request: bool = False,
    cancelled: bool = False,
    resource_pressure: bool = False,
) -> CalibrationPlan:
    return CalibrationPlan(
        decision=decide_benchmark_budget(
            policy=benchmark_policy_from_config(settings),
            intent=intent,
            predicted_encode_seconds=predicted_encode_seconds,
            explicit_request=explicit_request,
            cancelled=cancelled,
            resource_pressure=resource_pressure,
        )
    )
