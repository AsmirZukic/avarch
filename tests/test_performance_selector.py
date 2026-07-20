from __future__ import annotations

from dataclasses import dataclass

from avarch.application.candidate_tournament import CandidateKey, candidate_key
from avarch.application.history_estimator import (
    HistoricalCandidateEstimate,
    HistoryEstimateConfidence,
)
from avarch.application.performance_candidates import (
    PerformanceCandidate,
    generate_safe_concurrency_candidates,
)
from avarch.application.performance_selector import select_estimated_candidate
from avarch.domain.resource_policy import parse_resource_intent
from avarch.domain.scheduler import ResourceCapacity


def test_selector_rejects_fast_but_safety_constrained_candidate() -> None:
    candidates = _candidates()
    fast = _first_manual_key(candidates)

    selection = select_estimated_candidate(
        candidates=candidates,
        estimates=(
            _estimate(CandidateKey(workers="auto", svt_lp="native"), fps=20),
            _estimate(fast, fps=40, safety_constrained=True),
        ),
        capacity=_capacity(),
    )

    assert selection is not None
    assert candidate_key(selection.candidate) == CandidateKey(workers="auto", svt_lp="native")


def test_selector_requires_gain_over_native_baseline() -> None:
    candidates = _candidates()
    challenger = _first_manual_key(candidates)

    selection = select_estimated_candidate(
        candidates=candidates,
        estimates=(
            _estimate(CandidateKey(workers="auto", svt_lp="native"), fps=100),
            _estimate(challenger, fps=102),
        ),
        capacity=_capacity(),
        minimum_gain_fraction=0.03,
    )

    assert selection is not None
    assert candidate_key(selection.candidate) == CandidateKey(workers="auto", svt_lp="native")
    assert selection.reason == "native_baseline_uncertain_gain"


def test_selector_chooses_high_confidence_safe_candidate() -> None:
    candidates = _candidates()
    challenger = _first_manual_key(candidates)

    selection = select_estimated_candidate(
        candidates=candidates,
        estimates=(
            _estimate(CandidateKey(workers="auto", svt_lp="native"), fps=20),
            _estimate(challenger, fps=30),
        ),
        capacity=_capacity(),
    )

    assert selection is not None
    assert candidate_key(selection.candidate) == challenger
    assert selection.reason == "historical_estimate"
    assert selection.confidence == 0.9


def test_selector_filters_candidates_exceeding_global_memory_budget() -> None:
    candidates = _candidates(memory_budget_bytes=16 * 1024**3)
    challenger = _first_manual_key(candidates)

    selection = select_estimated_candidate(
        candidates=candidates,
        estimates=(
            _estimate(CandidateKey(workers="auto", svt_lp="native"), fps=20),
            _estimate(challenger, fps=40),
        ),
        capacity=_capacity(memory_budget_bytes=1),
    )

    assert selection is not None
    assert candidate_key(selection.candidate) == CandidateKey(workers="auto", svt_lp="native")


def _candidates(memory_budget_bytes: int = 32 * 1024**3) -> tuple[PerformanceCandidate, ...]:
    return generate_safe_concurrency_candidates(
        intent=parse_resource_intent(workers="auto"),
        capacity=_capacity(memory_budget_bytes=memory_budget_bytes),
        observations=(_Observation(peak_memory_bytes=1 * 1024**3),),
    )


def _capacity(memory_budget_bytes: int = 32 * 1024**3) -> ResourceCapacity:
    return ResourceCapacity(
        cheap_workers=1,
        av1an_jobs=1,
        file_ops=1,
        cpu_budget=8,
        memory_budget_bytes=memory_budget_bytes,
    )


def _estimate(
    key: CandidateKey,
    *,
    fps: float,
    confidence: HistoryEstimateConfidence = HistoryEstimateConfidence.HIGH,
    safety_constrained: bool = False,
) -> HistoricalCandidateEstimate:
    return HistoricalCandidateEstimate(
        candidate_key=key,
        median_fps=fps,
        peak_memory_bytes=1 * 1024**3,
        confidence=confidence,
        evidence_count=3,
        safety_constrained=safety_constrained,
    )


def _first_manual_key(candidates: tuple[PerformanceCandidate, ...]) -> CandidateKey:
    return next(candidate_key(candidate) for candidate in candidates if candidate.workers != "auto")


@dataclass(frozen=True, slots=True)
class _Observation:
    peak_memory_bytes: int | None
