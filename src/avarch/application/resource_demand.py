from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from avarch.application.resource_decision import ExecutionResourceDecision


class DemandConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ResourceDemandObservation(Protocol):
    @property
    def peak_memory_bytes(self) -> int | None: ...


@dataclass(frozen=True, slots=True)
class ResourceDemandRange:
    minimum: float | int | None
    maximum: float | int | None
    confidence: DemandConfidence


@dataclass(frozen=True, slots=True)
class EncodeResourceDemand:
    cpu: ResourceDemandRange
    memory_bytes: ResourceDemandRange
    requires_exclusive_heavy_job: bool
    reason: str


class ResourceDemandEstimator:
    def __init__(self, *, memory_safety_factor: float = 1.25) -> None:
        if not math.isfinite(memory_safety_factor) or memory_safety_factor < 1:
            raise ValueError("memory_safety_factor must be finite and at least 1")
        self._memory_safety_factor = memory_safety_factor

    def estimate(
        self,
        *,
        decision: ExecutionResourceDecision,
        observations: Iterable[ResourceDemandObservation] = (),
    ) -> EncodeResourceDemand:
        cpu = _cpu_demand(decision)
        memory = self._memory_demand(observations)
        exclusive = (
            decision.effective_workers == "auto"
            or cpu.maximum is None
            or memory.maximum is None
            or decision.fallback
        )
        reason = "exclusive_unknown_demand" if exclusive else "bounded_manual_demand"
        return EncodeResourceDemand(
            cpu=cpu,
            memory_bytes=memory,
            requires_exclusive_heavy_job=exclusive,
            reason=reason,
        )

    def _memory_demand(
        self,
        observations: Iterable[ResourceDemandObservation],
    ) -> ResourceDemandRange:
        peaks = [
            observation.peak_memory_bytes
            for observation in observations
            if observation.peak_memory_bytes is not None
        ]
        if not peaks:
            return ResourceDemandRange(
                minimum=None,
                maximum=None,
                confidence=DemandConfidence.LOW,
            )
        peak = max(peaks)
        return ResourceDemandRange(
            minimum=peak,
            maximum=math.ceil(peak * self._memory_safety_factor),
            confidence=DemandConfidence.MEDIUM,
        )


def _cpu_demand(decision: ExecutionResourceDecision) -> ResourceDemandRange:
    if decision.effective_workers == "auto":
        return ResourceDemandRange(
            minimum=None,
            maximum=None,
            confidence=DemandConfidence.LOW,
        )
    worker_count = int(decision.effective_workers)
    lp = decision.effective_svt_lp
    if isinstance(lp, int):
        demand = worker_count * lp
        return ResourceDemandRange(
            minimum=demand,
            maximum=demand,
            confidence=DemandConfidence.HIGH,
        )
    return ResourceDemandRange(
        minimum=worker_count,
        maximum=None,
        confidence=DemandConfidence.LOW,
    )
