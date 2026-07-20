from __future__ import annotations

from dataclasses import dataclass

from avarch.application.candidate_tournament import CandidateKey, candidate_key
from avarch.application.history_estimator import (
    HistoricalCandidateEstimate,
    HistoryEstimateConfidence,
)
from avarch.application.performance_candidates import (
    CandidateSvtLpValue,
    CandidateWorkerValue,
    PerformanceCandidate,
)
from avarch.domain.scheduler import JobResourceReservation, ResourceCapacity


@dataclass(frozen=True, slots=True)
class EstimatedResourceSelection:
    candidate: PerformanceCandidate
    confidence: float
    reason: str
    evidence_count: int
    estimated_fps: float

    @property
    def effective_workers(self) -> CandidateWorkerValue:
        return self.candidate.workers

    @property
    def effective_svt_lp(self) -> CandidateSvtLpValue:
        return self.candidate.svt_lp


def select_estimated_candidate(
    *,
    candidates: tuple[PerformanceCandidate, ...],
    estimates: tuple[HistoricalCandidateEstimate, ...],
    capacity: ResourceCapacity,
    minimum_gain_fraction: float = 0.03,
) -> EstimatedResourceSelection | None:
    candidate_by_key = {candidate_key(candidate): candidate for candidate in candidates}
    usable = [
        estimate
        for estimate in estimates
        if estimate.candidate_key in candidate_by_key
        and estimate.median_fps is not None
        and not estimate.safety_constrained
        and _confidence_value(estimate.confidence)
        >= _confidence_value(HistoryEstimateConfidence.MEDIUM)
        and _fits_capacity(candidate_by_key[estimate.candidate_key].reservation, capacity)
    ]
    if not usable:
        return None

    native_key = CandidateKey(workers="auto", svt_lp="native")
    native = next((estimate for estimate in usable if estimate.candidate_key == native_key), None)
    best = max(usable, key=lambda estimate: (estimate.median_fps or 0.0, -estimate.evidence_count))
    if native is not None and best.candidate_key != native_key:
        assert best.median_fps is not None
        assert native.median_fps is not None
        if best.median_fps < native.median_fps * (1 + minimum_gain_fraction):
            return _selection(
                candidate=candidate_by_key[native_key],
                estimate=native,
                reason="native_baseline_uncertain_gain",
            )
    return _selection(
        candidate=candidate_by_key[best.candidate_key],
        estimate=best,
        reason="historical_estimate",
    )


def _selection(
    *,
    candidate: PerformanceCandidate,
    estimate: HistoricalCandidateEstimate,
    reason: str,
) -> EstimatedResourceSelection:
    if estimate.median_fps is None:
        raise ValueError("selection requires throughput estimate")
    return EstimatedResourceSelection(
        candidate=candidate,
        confidence=_confidence_value(estimate.confidence),
        reason=reason,
        evidence_count=estimate.evidence_count,
        estimated_fps=estimate.median_fps,
    )


def _fits_capacity(
    reservation: JobResourceReservation,
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


def _confidence_value(confidence: HistoryEstimateConfidence) -> float:
    return {
        HistoryEstimateConfidence.HIGH: 0.9,
        HistoryEstimateConfidence.MEDIUM: 0.6,
        HistoryEstimateConfidence.LOW: 0.2,
    }[confidence]
