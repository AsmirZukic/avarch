from __future__ import annotations

from dataclasses import dataclass

from avarch.application.candidate_tournament import (
    CandidateKey,
    CandidateMeasurement,
    TournamentReason,
    candidate_key,
    next_measurement_candidates,
    select_measured_candidate,
)
from avarch.application.performance_candidates import (
    PerformanceCandidate,
    generate_safe_concurrency_candidates,
)
from avarch.domain.resource_policy import parse_resource_intent
from avarch.domain.scheduler import ResourceCapacity


def test_tournament_eliminates_unsafe_candidates_and_selects_fastest() -> None:
    candidates = _candidates()
    native = candidate_key(candidates[0])
    challenger = _manual_key(candidates)

    result = select_measured_candidate(
        candidates=candidates,
        measurements=(
            CandidateMeasurement(candidate_key=native, frames=100, duration_seconds=10),
            CandidateMeasurement(candidate_key=challenger, frames=100, duration_seconds=5),
            CandidateMeasurement(
                candidate_key=CandidateKey(workers=8, svt_lp=1),
                frames=0,
                duration_seconds=2,
                resource_pressure=True,
            ),
        ),
    )

    assert result.winner == challenger
    assert CandidateKey(workers=8, svt_lp=1) in result.eliminated
    assert result.reason == TournamentReason.WINNER
    assert result.confidence == 0.5


def test_high_confidence_requires_repeated_winner_measurement() -> None:
    candidates = _candidates()
    native = candidate_key(candidates[0])
    challenger = _manual_key(candidates)

    result = select_measured_candidate(
        candidates=candidates,
        measurements=(
            CandidateMeasurement(candidate_key=native, frames=100, duration_seconds=10),
            CandidateMeasurement(candidate_key=challenger, frames=100, duration_seconds=5),
            CandidateMeasurement(candidate_key=challenger, frames=120, duration_seconds=6),
        ),
    )

    assert result.winner == challenger
    assert result.confidence == 0.8


def test_native_baseline_wins_ties_and_uncertain_small_gains() -> None:
    candidates = _candidates()
    native = candidate_key(candidates[0])
    challenger = _manual_key(candidates)

    result = select_measured_candidate(
        candidates=candidates,
        measurements=(
            CandidateMeasurement(candidate_key=native, frames=100, duration_seconds=10),
            CandidateMeasurement(candidate_key=challenger, frames=102, duration_seconds=10),
        ),
        minimum_gain_fraction=0.03,
    )

    assert result.winner == native
    assert result.reason == TournamentReason.NATIVE_BASELINE


def test_more_measurement_is_assigned_only_to_close_finalists_with_budget() -> None:
    candidates = _candidates()
    native = candidate_key(candidates[0])
    first = _manual_key(candidates)
    second = next(
        candidate_key(candidate)
        for candidate in candidates
        if candidate.workers != "auto" and candidate_key(candidate) != first
    )

    finalists = next_measurement_candidates(
        candidates=candidates,
        measurements=(
            CandidateMeasurement(candidate_key=native, frames=100, duration_seconds=20),
            CandidateMeasurement(candidate_key=first, frames=100, duration_seconds=10),
            CandidateMeasurement(candidate_key=second, frames=96, duration_seconds=10),
        ),
        remaining_budget_seconds=30,
        close_fraction=0.05,
    )
    exhausted = next_measurement_candidates(
        candidates=candidates,
        measurements=(),
        remaining_budget_seconds=0,
    )

    assert finalists == tuple(sorted((first, second), key=lambda key: (key.workers, key.svt_lp)))
    assert exhausted == ()


def test_tournament_reports_insufficient_evidence_when_no_measurements_exist() -> None:
    result = select_measured_candidate(candidates=_candidates(), measurements=())

    assert result.winner is None
    assert result.reason == TournamentReason.INSUFFICIENT_EVIDENCE


def _candidates() -> tuple[PerformanceCandidate, ...]:
    return generate_safe_concurrency_candidates(
        intent=parse_resource_intent(workers="auto"),
        capacity=ResourceCapacity(
            cheap_workers=1,
            av1an_jobs=1,
            file_ops=1,
            cpu_budget=8,
            memory_budget_bytes=32 * 1024**3,
        ),
        observations=(_Observation(peak_memory_bytes=1 * 1024**3),),
    )


def _manual_key(candidates: tuple[PerformanceCandidate, ...]) -> CandidateKey:
    return next(candidate_key(candidate) for candidate in candidates if candidate.workers != "auto")


@dataclass(frozen=True, slots=True)
class _Observation:
    peak_memory_bytes: int | None
