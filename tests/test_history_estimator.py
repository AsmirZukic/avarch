from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from avarch.application.candidate_tournament import CandidateKey
from avarch.application.history_estimator import (
    HistoryEstimateConfidence,
    estimate_candidates_from_history,
)
from avarch.application.resource_decision import RESOURCE_DECISION_ALGORITHM_VERSION
from avarch.serialization import canonical_json

NOW = datetime(2026, 7, 1, 12, tzinfo=UTC)


def test_exact_history_groups_candidates_and_uses_median_throughput() -> None:
    estimates = estimate_candidates_from_history(
        (
            _observation(workers=2, svt_lp=4, fps=20),
            _observation(workers=2, svt_lp=4, fps=30),
            _observation(workers=2, svt_lp=4, fps=40),
            _observation(workers=1, svt_lp=4, fps=15),
        ),
        now=NOW,
    )

    assert estimates[0].candidate_key == CandidateKey(workers=2, svt_lp=4)
    assert estimates[0].median_fps == 30.0
    assert estimates[0].peak_memory_bytes == 2 * 1024**3
    assert estimates[0].confidence is HistoryEstimateConfidence.HIGH


def test_semantic_history_has_reduced_confidence() -> None:
    estimates = estimate_candidates_from_history(
        (
            _observation(workers=2, svt_lp=4, fps=20, match_quality="semantic"),
            _observation(workers=2, svt_lp=4, fps=30, match_quality="semantic"),
            _observation(workers=2, svt_lp=4, fps=40, match_quality="semantic"),
        ),
        now=NOW,
    )

    assert estimates[0].confidence is HistoryEstimateConfidence.MEDIUM


def test_old_history_decays_to_low_confidence() -> None:
    estimates = estimate_candidates_from_history(
        (
            _observation(workers=2, svt_lp=4, fps=20, created_at=NOW - timedelta(days=200)),
            _observation(workers=2, svt_lp=4, fps=30, created_at=NOW - timedelta(days=200)),
            _observation(workers=2, svt_lp=4, fps=40, created_at=NOW - timedelta(days=200)),
        ),
        now=NOW,
    )

    assert estimates[0].confidence is HistoryEstimateConfidence.LOW


def test_pressured_history_constrains_safety_without_advertising_throughput() -> None:
    estimates = estimate_candidates_from_history(
        (
            _observation(workers=4, svt_lp=4, fps=0, resource_pressure=True),
        ),
        now=NOW,
    )

    assert estimates[0].candidate_key == CandidateKey(workers=4, svt_lp=4)
    assert estimates[0].median_fps is None
    assert estimates[0].safety_constrained is True
    assert estimates[0].confidence is HistoryEstimateConfidence.LOW


def test_native_auto_history_is_retained_as_baseline_candidate() -> None:
    estimates = estimate_candidates_from_history(
        (
            _observation(workers="auto", svt_lp="native", fps=24),
        ),
        now=NOW,
    )

    assert estimates[0].candidate_key == CandidateKey(workers="auto", svt_lp="native")


def test_corrupt_history_payload_is_ignored() -> None:
    estimates = estimate_candidates_from_history(
        (
            _Observation(
                aggregate_fps=99,
                peak_memory_bytes=None,
                match_quality="exact",
                resource_decision_json="{not-json",
                created_at=NOW,
            ),
            _observation(workers=2, svt_lp=4, fps=24),
        ),
        now=NOW,
    )

    assert len(estimates) == 1
    assert estimates[0].candidate_key == CandidateKey(workers=2, svt_lp=4)


def test_history_from_an_older_selection_algorithm_is_ignored() -> None:
    observation = _observation(workers=2, svt_lp=4, fps=99)
    payload = {
        "algorithm_version": RESOURCE_DECISION_ALGORITHM_VERSION - 1,
        "effective_workers": 2,
        "effective_svt_lp": 4,
    }

    estimates = estimate_candidates_from_history(
        (replace(observation, resource_decision_json=canonical_json(payload)),),
        now=NOW,
    )

    assert estimates == ()


@dataclass(frozen=True, slots=True)
class _Observation:
    aggregate_fps: float
    peak_memory_bytes: int | None
    match_quality: str
    resource_decision_json: str | None
    created_at: datetime


def _observation(
    *,
    workers: int | str,
    svt_lp: int | str,
    fps: float,
    match_quality: str = "exact",
    created_at: datetime = NOW,
    resource_pressure: bool = False,
) -> _Observation:
    return _Observation(
        aggregate_fps=fps,
        peak_memory_bytes=2 * 1024**3,
        match_quality=match_quality,
        resource_decision_json=canonical_json(
            {
                "algorithm_version": RESOURCE_DECISION_ALGORITHM_VERSION,
                "effective_workers": workers,
                "effective_svt_lp": svt_lp,
                "resource_pressure": resource_pressure,
            }
        ),
        created_at=created_at,
    )
