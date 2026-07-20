from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from avarch.application.performance_candidates import (
    CandidateSvtLpValue,
    CandidateWorkerValue,
    PerformanceCandidate,
)


class TournamentReason(StrEnum):
    WINNER = "winner"
    NATIVE_BASELINE = "native_baseline"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    ALL_UNSAFE = "all_unsafe"


@dataclass(frozen=True, slots=True)
class CandidateKey:
    workers: CandidateWorkerValue
    svt_lp: CandidateSvtLpValue


@dataclass(frozen=True, slots=True)
class CandidateMeasurement:
    candidate_key: CandidateKey
    frames: float
    duration_seconds: float
    failed: bool = False
    resource_pressure: bool = False


@dataclass(frozen=True, slots=True)
class CandidateTournamentResult:
    winner: CandidateKey | None
    eliminated: tuple[CandidateKey, ...]
    finalists: tuple[CandidateKey, ...]
    confidence: float
    measurement_cost_seconds: float
    reason: TournamentReason


def candidate_key(candidate: PerformanceCandidate) -> CandidateKey:
    return CandidateKey(workers=candidate.workers, svt_lp=candidate.svt_lp)


def select_measured_candidate(
    *,
    candidates: tuple[PerformanceCandidate, ...],
    measurements: Iterable[CandidateMeasurement],
    minimum_gain_fraction: float = 0.03,
) -> CandidateTournamentResult:
    if minimum_gain_fraction < 0 or not math.isfinite(minimum_gain_fraction):
        raise ValueError("minimum_gain_fraction must be finite and non-negative")
    keys = tuple(candidate_key(candidate) for candidate in candidates)
    measurement_tuple = tuple(measurements)
    cost = sum(
        measurement.duration_seconds
        for measurement in measurement_tuple
        if math.isfinite(measurement.duration_seconds) and measurement.duration_seconds > 0
    )
    unsafe = {
        measurement.candidate_key
        for measurement in measurement_tuple
        if measurement.failed or measurement.resource_pressure
    }
    scored = {
        key: _throughput(
            measurement
            for measurement in measurement_tuple
            if measurement.candidate_key == key and key not in unsafe
        )
        for key in keys
    }
    scored = {key: value for key, value in scored.items() if value is not None}
    if not scored:
        reason = (
            TournamentReason.ALL_UNSAFE
            if unsafe
            else TournamentReason.INSUFFICIENT_EVIDENCE
        )
        return CandidateTournamentResult(
            winner=None,
            eliminated=tuple(sorted(unsafe, key=_sort_key)),
            finalists=(),
            confidence=0.0,
            measurement_cost_seconds=cost,
            reason=reason,
        )

    native_key = CandidateKey(workers="auto", svt_lp="native")
    native_score = scored.get(native_key)
    best_key, best_score = max(scored.items(), key=lambda item: (item[1], _sort_key(item[0])))
    successful_measurement_counts = {
        key: sum(
            1
            for measurement in measurement_tuple
            if measurement.candidate_key == key
            and not measurement.failed
            and not measurement.resource_pressure
        )
        for key in keys
    }
    eliminated = tuple(sorted((set(keys) - {best_key}) | unsafe, key=_sort_key))
    finalists = _close_finalists(
        scored,
        best_score=best_score,
        close_fraction=minimum_gain_fraction,
    )
    if (
        native_score is not None
        and best_key != native_key
        and best_score < native_score * (1 + minimum_gain_fraction)
    ):
        return CandidateTournamentResult(
            winner=native_key,
            eliminated=tuple(sorted((set(keys) - {native_key}) | unsafe, key=_sort_key)),
            finalists=finalists,
            confidence=0.5,
            measurement_cost_seconds=cost,
            reason=TournamentReason.NATIVE_BASELINE,
        )
    return CandidateTournamentResult(
        winner=best_key,
        eliminated=eliminated,
        finalists=finalists,
        confidence=(
            0.8
            if len(finalists) == 1 and successful_measurement_counts.get(best_key, 0) >= 2
            else 0.5
        ),
        measurement_cost_seconds=cost,
        reason=TournamentReason.NATIVE_BASELINE
        if best_key == native_key
        else TournamentReason.WINNER,
    )


def next_measurement_candidates(
    *,
    candidates: tuple[PerformanceCandidate, ...],
    measurements: Iterable[CandidateMeasurement],
    remaining_budget_seconds: float,
    close_fraction: float = 0.05,
) -> tuple[CandidateKey, ...]:
    if remaining_budget_seconds <= 0:
        return ()
    result = select_measured_candidate(
        candidates=candidates,
        measurements=measurements,
        minimum_gain_fraction=close_fraction,
    )
    if result.reason in {TournamentReason.ALL_UNSAFE, TournamentReason.INSUFFICIENT_EVIDENCE}:
        measured_keys = {measurement.candidate_key for measurement in measurements}
        return tuple(
            candidate_key(candidate)
            for candidate in candidates
            if candidate_key(candidate) not in measured_keys
        )
    return result.finalists


def _throughput(measurements: Iterable[CandidateMeasurement]) -> float | None:
    frames = 0.0
    duration = 0.0
    for measurement in measurements:
        if measurement.failed or measurement.resource_pressure:
            continue
        if measurement.frames <= 0 or measurement.duration_seconds <= 0:
            continue
        frames += measurement.frames
        duration += measurement.duration_seconds
    if frames <= 0 or duration <= 0:
        return None
    return frames / duration


def _close_finalists(
    scores: dict[CandidateKey, float],
    *,
    best_score: float,
    close_fraction: float,
) -> tuple[CandidateKey, ...]:
    threshold = best_score * (1 - close_fraction)
    return tuple(
        sorted(
            (key for key, score in scores.items() if score >= threshold),
            key=_sort_key,
        )
    )


def _sort_key(key: CandidateKey) -> tuple[int, int, int]:
    worker_sort = 0 if key.workers == "auto" else int(key.workers)
    lp_sort = 0 if key.svt_lp == "native" else int(key.svt_lp)
    native_sort = 0 if key.workers == "auto" and key.svt_lp == "native" else 1
    return (native_sort, worker_sort, lp_sort)
