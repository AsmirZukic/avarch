from __future__ import annotations

from dataclasses import dataclass

from avarch.application.resource_decision import ExecutionResourceDecision
from avarch.application.resource_demand import DemandConfidence, ResourceDemandEstimator
from avarch.domain.resource_policy import ResourceMode


def test_manual_decision_uses_explicit_worker_and_svt_lp_cpu_demand() -> None:
    demand = ResourceDemandEstimator().estimate(
        decision=ExecutionResourceDecision(
            mode=ResourceMode.MANUAL,
            effective_workers=2,
            effective_svt_lp=4,
            reason="manual_override",
            confidence=1.0,
        ),
        observations=(_Observation(peak_memory_bytes=2 * 1024**3),),
    )

    assert demand.cpu.minimum == 8
    assert demand.cpu.maximum == 8
    assert demand.cpu.confidence is DemandConfidence.HIGH
    assert demand.requires_exclusive_heavy_job is False


def test_native_auto_unknown_demand_gets_exclusive_heavy_allocation() -> None:
    demand = ResourceDemandEstimator().estimate(
        decision=ExecutionResourceDecision(
            mode=ResourceMode.NATIVE,
            effective_workers="auto",
            effective_svt_lp="native",
            reason="native_requested",
            confidence=1.0,
        )
    )

    assert demand.cpu.maximum is None
    assert demand.memory_bytes.maximum is None
    assert demand.requires_exclusive_heavy_job is True
    assert demand.reason == "exclusive_unknown_demand"


def test_memory_demand_uses_historical_peak_with_safety_factor() -> None:
    demand = ResourceDemandEstimator(memory_safety_factor=1.5).estimate(
        decision=ExecutionResourceDecision(
            mode=ResourceMode.MANUAL,
            effective_workers=1,
            effective_svt_lp=2,
            reason="manual_override",
            confidence=1.0,
        ),
        observations=(
            _Observation(peak_memory_bytes=2 * 1024**3),
            _Observation(peak_memory_bytes=3 * 1024**3),
        ),
    )

    assert demand.memory_bytes.minimum == 3 * 1024**3
    assert demand.memory_bytes.maximum == int(4.5 * 1024**3)
    assert demand.memory_bytes.confidence is DemandConfidence.MEDIUM


def test_missing_history_cannot_produce_confident_low_memory_estimate() -> None:
    demand = ResourceDemandEstimator().estimate(
        decision=ExecutionResourceDecision(
            mode=ResourceMode.MANUAL,
            effective_workers=1,
            effective_svt_lp=2,
            reason="manual_override",
            confidence=1.0,
        )
    )

    assert demand.memory_bytes.minimum is None
    assert demand.memory_bytes.maximum is None
    assert demand.memory_bytes.confidence is DemandConfidence.LOW
    assert demand.requires_exclusive_heavy_job is True


@dataclass(frozen=True, slots=True)
class _Observation:
    peak_memory_bytes: int | None
