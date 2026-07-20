from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from avarch.application.resources import ResourceConfidence, ResourceSnapshot
from avarch.domain.resource_policy import ResourceIntent, ResourceMode

RESOURCE_DECISION_ALGORITHM_VERSION = 2


type WorkerDecisionValue = int | Literal["auto"]
type SvtDecisionValue = int | Literal["native"]


@dataclass(frozen=True, slots=True)
class ExecutionResourceDecision:
    mode: ResourceMode
    effective_workers: WorkerDecisionValue
    effective_svt_lp: SvtDecisionValue
    reason: str
    confidence: float
    algorithm_version: int = RESOURCE_DECISION_ALGORITHM_VERSION
    fallback: bool = False
    evidence_count: int | None = None


class ResourceSelectionEvidence(Protocol):
    @property
    def effective_workers(self) -> WorkerDecisionValue: ...

    @property
    def effective_svt_lp(self) -> SvtDecisionValue: ...

    @property
    def reason(self) -> str: ...

    @property
    def confidence(self) -> float: ...

    @property
    def evidence_count(self) -> int: ...


class ResourceDecisionService:
    def resolve(
        self,
        *,
        intent: ResourceIntent,
        snapshot: ResourceSnapshot,
        evidence: ResourceSelectionEvidence | None = None,
        supported_platform: bool = True,
    ) -> ExecutionResourceDecision:
        if intent.mode is ResourceMode.MANUAL:
            return ExecutionResourceDecision(
                mode=ResourceMode.MANUAL,
                effective_workers=int(intent.workers.value),
                effective_svt_lp=int(intent.svt_lp.value)
                if isinstance(intent.svt_lp.value, int)
                else "native",
                reason="manual_override",
                confidence=1.0,
                fallback=False,
            )
        if intent.mode is ResourceMode.NATIVE:
            return _native_decision(reason="native_requested", confidence=1.0, fallback=False)
        if evidence is not None and evidence.confidence >= 0.6:
            return ExecutionResourceDecision(
                mode=ResourceMode.AUTO
                if evidence.effective_workers != "auto"
                else ResourceMode.NATIVE,
                effective_workers=evidence.effective_workers,
                effective_svt_lp=evidence.effective_svt_lp,
                reason=evidence.reason,
                confidence=evidence.confidence,
                fallback=False,
                evidence_count=evidence.evidence_count,
            )
        if not supported_platform or _snapshot_is_low_confidence(snapshot):
            return _native_decision(
                reason="unsupported_or_low_confidence_resources",
                confidence=0.0,
                fallback=True,
            )
        return _native_decision(reason="insufficient_evidence", confidence=0.0, fallback=True)


def _native_decision(
    *,
    reason: str,
    confidence: float,
    fallback: bool,
) -> ExecutionResourceDecision:
    return ExecutionResourceDecision(
        mode=ResourceMode.NATIVE,
        effective_workers="auto",
        effective_svt_lp="native",
        reason=reason,
        confidence=confidence,
        fallback=fallback,
    )


def _snapshot_is_low_confidence(snapshot: ResourceSnapshot) -> bool:
    values = (*snapshot.cpu_values, *snapshot.memory_values)
    if snapshot.effective_cpu_count is None or snapshot.effective_memory_bytes is None:
        return True
    return any(value.confidence is not ResourceConfidence.HIGH for value in values)
