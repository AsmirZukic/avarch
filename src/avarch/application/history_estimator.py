from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from statistics import median
from typing import Any, Protocol, cast

from avarch.application.candidate_tournament import CandidateKey
from avarch.application.resource_decision import RESOURCE_DECISION_ALGORITHM_VERSION


class HistoryEstimateConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class HistoricalObservation(Protocol):
    @property
    def aggregate_fps(self) -> float: ...

    @property
    def peak_memory_bytes(self) -> int | None: ...

    @property
    def match_quality(self) -> str: ...

    @property
    def resource_decision_json(self) -> str | None: ...

    @property
    def created_at(self) -> datetime: ...


@dataclass(frozen=True, slots=True)
class HistoricalCandidateEstimate:
    candidate_key: CandidateKey
    median_fps: float | None
    peak_memory_bytes: int | None
    confidence: HistoryEstimateConfidence
    evidence_count: int
    safety_constrained: bool


def estimate_candidates_from_history(
    observations: Iterable[HistoricalObservation],
    *,
    now: datetime,
) -> tuple[HistoricalCandidateEstimate, ...]:
    grouped: dict[CandidateKey, list[HistoricalObservation]] = {}
    safety_only: dict[CandidateKey, list[HistoricalObservation]] = {}
    for observation in observations:
        key = _candidate_key_from_observation(observation)
        if key is None:
            continue
        if _pressured(observation):
            safety_only.setdefault(key, []).append(observation)
            continue
        grouped.setdefault(key, []).append(observation)

    estimates = [
        _estimate(key=key, observations=values, now=now, safety_constrained=key in safety_only)
        for key, values in grouped.items()
    ]
    for key, values in safety_only.items():
        if key not in grouped:
            estimates.append(
                HistoricalCandidateEstimate(
                    candidate_key=key,
                    median_fps=None,
                    peak_memory_bytes=_peak_memory(values),
                    confidence=HistoryEstimateConfidence.LOW,
                    evidence_count=len(values),
                    safety_constrained=True,
                )
            )
    return tuple(
        sorted(
            estimates,
            key=lambda estimate: (
                _confidence_sort(estimate.confidence),
                -(estimate.median_fps or 0.0),
                _candidate_sort_key(estimate.candidate_key),
            ),
        )
    )


def _estimate(
    *,
    key: CandidateKey,
    observations: list[HistoricalObservation],
    now: datetime,
    safety_constrained: bool,
) -> HistoricalCandidateEstimate:
    fps_values = [observation.aggregate_fps for observation in observations]
    confidence = _confidence(observations=observations, now=now)
    if safety_constrained and confidence is HistoryEstimateConfidence.HIGH:
        confidence = HistoryEstimateConfidence.MEDIUM
    return HistoricalCandidateEstimate(
        candidate_key=key,
        median_fps=float(median(fps_values)) if fps_values else None,
        peak_memory_bytes=_peak_memory(observations),
        confidence=confidence,
        evidence_count=len(observations),
        safety_constrained=safety_constrained,
    )


def _confidence(
    *,
    observations: list[HistoricalObservation],
    now: datetime,
) -> HistoryEstimateConfidence:
    exact_count = sum(1 for observation in observations if observation.match_quality == "exact")
    newest_age = min(
        (_age(now, observation.created_at) for observation in observations),
        default=None,
    )
    if newest_age is not None and newest_age > timedelta(days=180):
        return HistoryEstimateConfidence.LOW
    if exact_count >= 3:
        return HistoryEstimateConfidence.HIGH
    if exact_count >= 1 or len(observations) >= 3:
        return HistoryEstimateConfidence.MEDIUM
    return HistoryEstimateConfidence.LOW


def _age(now: datetime, created_at: datetime) -> timedelta:
    if now.tzinfo is not None and created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=now.tzinfo)
    elif now.tzinfo is None and created_at.tzinfo is not None:
        now = now.replace(tzinfo=created_at.tzinfo)
    return now - created_at


def _candidate_key_from_observation(observation: HistoricalObservation) -> CandidateKey | None:
    if observation.resource_decision_json is None:
        return None
    try:
        payload = json.loads(observation.resource_decision_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    payload_dict = cast(dict[str, Any], payload)
    if payload_dict.get("algorithm_version") != RESOURCE_DECISION_ALGORITHM_VERSION:
        return None
    workers = payload_dict.get("effective_workers")
    svt_lp = payload_dict.get("effective_svt_lp")
    if workers == "auto" and svt_lp == "native":
        return CandidateKey(workers="auto", svt_lp="native")
    if isinstance(workers, int) and isinstance(svt_lp, int):
        return CandidateKey(workers=workers, svt_lp=svt_lp)
    return None


def _pressured(observation: HistoricalObservation) -> bool:
    try:
        payload = json.loads(observation.resource_decision_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return False
    if not isinstance(payload, dict):
        return False
    payload_dict = cast(dict[str, Any], payload)
    return bool(payload_dict.get("resource_pressure") or payload_dict.get("oom"))


def _peak_memory(observations: Iterable[HistoricalObservation]) -> int | None:
    peaks = [observation.peak_memory_bytes for observation in observations]
    available = [peak for peak in peaks if peak is not None]
    return max(available) if available else None


def _confidence_sort(confidence: HistoryEstimateConfidence) -> int:
    return {
        HistoryEstimateConfidence.HIGH: 0,
        HistoryEstimateConfidence.MEDIUM: 1,
        HistoryEstimateConfidence.LOW: 2,
    }[confidence]


def _candidate_sort_key(key: CandidateKey) -> tuple[int, int, int]:
    native_sort = 0 if key.workers == "auto" and key.svt_lp == "native" else 1
    workers = 0 if key.workers == "auto" else int(key.workers)
    lp = 0 if key.svt_lp == "native" else int(key.svt_lp)
    return (native_sort, workers, lp)
