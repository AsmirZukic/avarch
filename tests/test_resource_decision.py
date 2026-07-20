from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from avarch.application.resource_decision import ResourceDecisionService
from avarch.application.resources import ResourceConfidence, ResourceSnapshot, ResourceValue
from avarch.domain.resource_policy import ResourceMode, parse_resource_intent


def test_manual_intent_resolves_exactly() -> None:
    decision = ResourceDecisionService().resolve(
        intent=parse_resource_intent(mode="manual", workers=2, svt_lp=4),
        snapshot=_snapshot(),
    )

    assert decision.mode is ResourceMode.MANUAL
    assert decision.effective_workers == 2
    assert decision.effective_svt_lp == 4
    assert decision.reason == "manual_override"
    assert decision.confidence == 1.0
    assert decision.fallback is False


def test_native_intent_resolves_to_av1an_and_svt_native_auto() -> None:
    decision = ResourceDecisionService().resolve(
        intent=parse_resource_intent(mode="native", workers="auto"),
        snapshot=_snapshot(),
    )

    assert decision.mode is ResourceMode.NATIVE
    assert decision.effective_workers == "auto"
    assert decision.effective_svt_lp == "native"
    assert decision.reason == "native_requested"
    assert decision.fallback is False


def test_auto_intent_without_evidence_falls_back_to_native() -> None:
    decision = ResourceDecisionService().resolve(
        intent=parse_resource_intent(workers="auto"),
        snapshot=_snapshot(),
    )

    assert decision.mode is ResourceMode.NATIVE
    assert decision.reason == "insufficient_evidence"
    assert decision.confidence == 0.0
    assert decision.fallback is True


def test_auto_intent_on_low_confidence_snapshot_falls_back_to_native() -> None:
    decision = ResourceDecisionService().resolve(
        intent=parse_resource_intent(workers="auto"),
        snapshot=_snapshot(cpu_confidence=ResourceConfidence.LOW),
        supported_platform=False,
    )

    assert decision.effective_workers == "auto"
    assert decision.reason == "unsupported_or_low_confidence_resources"
    assert decision.fallback is True


def test_auto_intent_with_confident_evidence_resolves_estimated_candidate() -> None:
    decision = ResourceDecisionService().resolve(
        intent=parse_resource_intent(workers="auto"),
        snapshot=_snapshot(),
        evidence=_Evidence(
            effective_workers=2,
            effective_svt_lp=4,
            reason="historical_estimate",
            confidence=0.9,
            evidence_count=3,
        ),
    )

    assert decision.mode is ResourceMode.AUTO
    assert decision.effective_workers == 2
    assert decision.effective_svt_lp == 4
    assert decision.reason == "historical_estimate"
    assert decision.confidence == 0.9
    assert decision.evidence_count == 3
    assert decision.fallback is False


def test_auto_intent_with_low_confidence_evidence_falls_back_to_native() -> None:
    decision = ResourceDecisionService().resolve(
        intent=parse_resource_intent(workers="auto"),
        snapshot=_snapshot(),
        evidence=_Evidence(
            effective_workers=2,
            effective_svt_lp=4,
            reason="historical_estimate",
            confidence=0.2,
            evidence_count=1,
        ),
    )

    assert decision.mode is ResourceMode.NATIVE
    assert decision.reason == "insufficient_evidence"
    assert decision.fallback is True


@dataclass(frozen=True, slots=True)
class _Evidence:
    effective_workers: int | Literal["auto"]
    effective_svt_lp: int | Literal["native"]
    reason: str
    confidence: float
    evidence_count: int


def _snapshot(
    *,
    cpu_confidence: ResourceConfidence = ResourceConfidence.HIGH,
) -> ResourceSnapshot:
    return ResourceSnapshot(
        effective_cpu_count=4,
        effective_cpu_quota=4.0,
        effective_memory_bytes=8 * 1024**3,
        cpu_values=(ResourceValue("cgroup_v2", 4.0, cpu_confidence),),
        memory_values=(ResourceValue("cgroup_v2", 8 * 1024**3, ResourceConfidence.HIGH),),
    )
