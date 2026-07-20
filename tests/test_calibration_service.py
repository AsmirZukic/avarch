from __future__ import annotations

from avarch.application.calibration_service import plan_calibration
from avarch.config import PerformanceSettings
from avarch.domain.resource_policy import parse_resource_intent


def test_calibration_service_uses_configured_policy_limits() -> None:
    plan = plan_calibration(
        settings=PerformanceSettings(
            calibration_max_seconds=30,
            calibration_max_predicted_fraction=0.005,
            calibration_min_predicted_seconds=600,
        ),
        intent=parse_resource_intent(workers="auto"),
        predicted_encode_seconds=10_000,
    )

    assert plan.decision.should_benchmark is True
    assert plan.decision.budget_seconds == 30


def test_calibration_service_honors_disabled_config() -> None:
    plan = plan_calibration(
        settings=PerformanceSettings(calibration_enabled=False),
        intent=parse_resource_intent(workers="auto"),
        predicted_encode_seconds=10_000,
    )

    assert plan.decision.should_benchmark is False
    assert plan.decision.reason.value == "disabled"
