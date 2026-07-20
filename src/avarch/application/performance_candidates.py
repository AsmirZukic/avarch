from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Literal

from avarch.application.resource_decision import ExecutionResourceDecision
from avarch.application.resource_demand import (
    ResourceDemandEstimator,
    ResourceDemandObservation,
)
from avarch.domain.resource_policy import ResourceIntent, ResourceMode
from avarch.domain.scheduler import JobResourceReservation, ResourceCapacity

type CandidateWorkerValue = int | Literal["auto"]
type CandidateSvtLpValue = int | Literal["native"]


@dataclass(frozen=True, slots=True)
class PerformanceCandidate:
    workers: CandidateWorkerValue
    svt_lp: CandidateSvtLpValue
    decision: ExecutionResourceDecision
    reservation: JobResourceReservation
    reason: str


def generate_safe_concurrency_candidates(
    *,
    intent: ResourceIntent,
    capacity: ResourceCapacity,
    observations: tuple[ResourceDemandObservation, ...] = (),
    available_parallel_units: int | None = None,
    max_candidates: int = 8,
) -> tuple[PerformanceCandidate, ...]:
    if max_candidates <= 0:
        raise ValueError("max_candidates must be positive")
    if intent.mode is ResourceMode.MANUAL:
        return (_manual_candidate(intent=intent, observations=observations),)

    candidates: list[PerformanceCandidate] = [_native_candidate()]
    cpu_budget = _integer_cpu_budget(capacity)
    if cpu_budget is None:
        return tuple(candidates)

    max_workers = cpu_budget
    if available_parallel_units is not None:
        if available_parallel_units <= 0:
            return tuple(candidates)
        max_workers = min(max_workers, available_parallel_units)

    for workers, svt_lp in _balanced_worker_lp_values(
        cpu_budget=cpu_budget,
        max_workers=max_workers,
    ):
        candidate = _candidate(
            workers=workers,
            svt_lp=svt_lp,
            observations=observations,
        )
        if _fits_capacity(candidate.reservation, capacity=capacity):
            candidates.append(candidate)
        if len(candidates) >= max_candidates:
            return tuple(candidates)
    return tuple(candidates)


def _native_candidate() -> PerformanceCandidate:
    decision = ExecutionResourceDecision(
        mode=ResourceMode.NATIVE,
        effective_workers="auto",
        effective_svt_lp="native",
        reason="native_baseline",
        confidence=1.0,
    )
    return PerformanceCandidate(
        workers="auto",
        svt_lp="native",
        decision=decision,
        reservation=JobResourceReservation(exclusive=True),
        reason="native_baseline",
    )


def _manual_candidate(
    *,
    intent: ResourceIntent,
    observations: tuple[ResourceDemandObservation, ...],
) -> PerformanceCandidate:
    workers = int(intent.workers.value)
    svt_lp = int(intent.svt_lp.value) if isinstance(intent.svt_lp.value, int) else "native"
    return _candidate(
        workers=workers,
        svt_lp=svt_lp,
        observations=observations,
        mode=ResourceMode.MANUAL,
        reason="manual_override",
    )


def _candidate(
    *,
    workers: int,
    svt_lp: int | Literal["native"],
    observations: tuple[ResourceDemandObservation, ...],
    mode: ResourceMode = ResourceMode.AUTO,
    reason: str = "generated_safe_candidate",
) -> PerformanceCandidate:
    decision = ExecutionResourceDecision(
        mode=mode,
        effective_workers=workers,
        effective_svt_lp=svt_lp,
        reason=reason,
        confidence=0.5 if mode is ResourceMode.AUTO else 1.0,
    )
    demand = ResourceDemandEstimator().estimate(
        decision=decision,
        observations=observations,
    )
    reservation = (
        JobResourceReservation(exclusive=True)
        if demand.requires_exclusive_heavy_job
        else _bounded_reservation(
            cpu=demand.cpu.maximum,
            memory_bytes=demand.memory_bytes.maximum,
        )
    )
    return PerformanceCandidate(
        workers=workers,
        svt_lp=svt_lp,
        decision=decision,
        reservation=reservation,
        reason=reason,
    )


def _bounded_reservation(
    *,
    cpu: float | int | None,
    memory_bytes: float | int | None,
) -> JobResourceReservation:
    if cpu is None or memory_bytes is None:
        raise ValueError("bounded candidate requires CPU and memory demand")
    return JobResourceReservation(
        cpu=float(cpu),
        memory_bytes=int(memory_bytes),
        exclusive=False,
    )


def _integer_cpu_budget(capacity: ResourceCapacity) -> int | None:
    if capacity.cpu_budget is None:
        return None
    if not math.isfinite(capacity.cpu_budget) or capacity.cpu_budget < 1:
        return None
    return max(1, math.floor(capacity.cpu_budget))


def _fits_capacity(
    reservation: JobResourceReservation,
    *,
    capacity: ResourceCapacity,
) -> bool:
    if reservation.exclusive:
        return True
    if (
        capacity.cpu_budget is not None
        and reservation.cpu is not None
        and reservation.cpu > capacity.cpu_budget
    ):
        return False
    return not (
        capacity.memory_budget_bytes is not None
        and reservation.memory_bytes is not None
        and reservation.memory_bytes > capacity.memory_budget_bytes
    )


def _balanced_worker_lp_values(
    *,
    cpu_budget: int,
    max_workers: int,
) -> tuple[tuple[int, int], ...]:
    pairs = {
        (workers, max(1, cpu_budget // workers))
        for workers in range(2, max_workers + 1)
    }
    return tuple(
        sorted(
            pairs,
            key=lambda pair: (
                abs(pair[0] - pair[1]),
                -(pair[0] * pair[1]),
                pair[0],
            ),
        )
    )
