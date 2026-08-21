from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Protocol

from avarch.application.benchmark_samples import (
    BenchmarkSample,
    BenchmarkSamplePolicy,
    select_representative_sample,
)
from avarch.application.calibration_runner import (
    Av1anCommandRenderer,
    CalibrationMeasurementResult,
    CalibrationMeasurementStatus,
    CalibrationProcessRunner,
    measure_candidate,
)
from avarch.application.calibration_service import plan_calibration
from avarch.application.candidate_tournament import (
    CandidateKey,
    CandidateMeasurement,
    CandidateTournamentResult,
    candidate_key,
    next_measurement_candidates,
    select_measured_candidate,
)
from avarch.application.performance_candidates import (
    CandidateSvtLpValue,
    CandidateWorkerValue,
    PerformanceCandidate,
    generate_safe_concurrency_candidates,
)
from avarch.application.resource_demand import ResourceDemandObservation
from avarch.config import PerformanceSettings
from avarch.domain.resource_policy import ResourceIntent
from avarch.domain.scheduler import ResourceCapacity
from avarch.models.execution import ProcessCancellationToken
from avarch.models.plan import TranscodePlan


class CalibrationStatus(str):
    COMPLETED = "completed"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"
    INCOMPLETE = "incomplete"


class CalibrationStatusSink(Protocol):
    def __call__(self, message: str) -> None: ...


MIN_INITIAL_CANDIDATES = 5
MAX_FINALIST_ROUNDS = 3
FINALIST_CLOSE_FRACTION = 0.05
CALIBRATION_DEADLINE_GUARD_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class MeasuredResourceSelection:
    candidate: PerformanceCandidate
    confidence: float
    reason: str
    evidence_count: int

    @property
    def effective_workers(self) -> CandidateWorkerValue:
        return self.candidate.workers

    @property
    def effective_svt_lp(self) -> CandidateSvtLpValue:
        return self.candidate.svt_lp


@dataclass(frozen=True, slots=True)
class CalibrationExecutionResult:
    status: str
    reason: str
    budget_seconds: float
    sample: BenchmarkSample | None
    candidates: tuple[PerformanceCandidate, ...]
    measurements: tuple[CalibrationMeasurementResult, ...]
    tournament: CandidateTournamentResult | None
    selection: MeasuredResourceSelection | None

    @property
    def incomplete(self) -> bool:
        return self.status != CalibrationStatus.COMPLETED


def run_calibration(
    *,
    plan: TranscodePlan,
    settings: PerformanceSettings,
    intent: ResourceIntent,
    capacity: ResourceCapacity,
    observations: tuple[ResourceDemandObservation, ...],
    frame_rate: float | None,
    predicted_encode_seconds: float | None,
    calibration_dir: Path,
    runner: CalibrationProcessRunner,
    command_renderer: Av1anCommandRenderer,
    cancellation_token: ProcessCancellationToken,
    status_sink: CalibrationStatusSink | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> CalibrationExecutionResult:
    budget = plan_calibration(
        settings=settings,
        intent=intent,
        predicted_encode_seconds=predicted_encode_seconds,
    ).decision
    if not budget.should_benchmark:
        return _skipped(reason=budget.reason.value, budget_seconds=budget.budget_seconds)

    candidates = generate_safe_concurrency_candidates(
        intent=intent,
        capacity=capacity,
        observations=observations,
        max_candidates=settings.calibration_max_candidates,
    )
    selected_sample = select_representative_sample(
        duration_seconds=plan.validation.source_duration_seconds,
        frame_rate=frame_rate,
        identity_seed=plan.semantic_hash or plan.plan_hash,
        candidates=candidates,
        policy=BenchmarkSamplePolicy(
            target_sample_seconds=settings.calibration_sample_seconds,
            min_sample_seconds=min(10.0, settings.calibration_sample_seconds),
        ),
    )
    if selected_sample.sample is None:
        return _skipped(
            reason=selected_sample.reason.value,
            budget_seconds=budget.budget_seconds,
        )

    sample = selected_sample.sample
    candidates = selected_sample.candidates
    total_budget_seconds = settings.calibration_max_seconds
    measurements: list[CalibrationMeasurementResult] = []
    started_at = monotonic()
    calibration_dir.mkdir(parents=True, exist_ok=True)
    for index, candidate in enumerate(candidates, start=1):
        if cancellation_token.cancel_requested:
            break
        elapsed = monotonic() - started_at
        remaining_budget = max(0.0, total_budget_seconds - elapsed)
        if remaining_budget <= 0:
            break
        if status_sink is not None:
            status_sink(
                f"calibrating candidate {index}/{len(candidates)} "
                f"(workers={candidate.workers}, lp={candidate.svt_lp})"
            )
        result = measure_candidate(
            plan=plan,
            candidate=candidate,
            sample=sample,
            calibration_dir=calibration_dir,
            runner=runner,
            command_renderer=command_renderer,
            cancellation_token=cancellation_token,
            warmup_seconds=settings.calibration_warmup_seconds,
            timeout_seconds=remaining_budget,
        )
        measurements.append(result)
        _discard_candidate_payload(result)
        if status_sink is not None:
            status_sink(
                f"calibration candidate {index}/{len(candidates)} {result.status.value}"
            )
        if result.status is CalibrationMeasurementStatus.CANCELLED:
            break

    if cancellation_token.cancel_requested:
        return CalibrationExecutionResult(
            status=CalibrationStatus.CANCELLED,
            reason="cancelled",
            budget_seconds=total_budget_seconds,
            sample=sample,
            candidates=candidates,
            measurements=tuple(measurements),
            tournament=None,
            selection=None,
        )

    completed_keys = _completed_candidate_keys(measurements)
    required_initial_candidates = min(MIN_INITIAL_CANDIDATES, len(candidates))
    if len(completed_keys) < required_initial_candidates:
        return CalibrationExecutionResult(
            status=CalibrationStatus.INCOMPLETE,
            reason="candidate_set_incomplete",
            budget_seconds=total_budget_seconds,
            sample=sample,
            candidates=candidates,
            measurements=tuple(measurements),
            tournament=None,
            selection=None,
        )

    _remeasure_finalists(
        plan=plan,
        candidates=candidates,
        sample=sample,
        measurements=measurements,
        calibration_dir=calibration_dir,
        runner=runner,
        command_renderer=command_renderer,
        cancellation_token=cancellation_token,
        warmup_seconds=settings.calibration_warmup_seconds,
        total_budget_seconds=total_budget_seconds,
        started_at=started_at,
        monotonic=monotonic,
        status_sink=status_sink,
    )
    tournament_measurements = tuple(_tournament_measurement(item, sample) for item in measurements)
    completed_keys = _completed_measurement_keys(tournament_measurements)
    if len(completed_keys) < 2:
        return CalibrationExecutionResult(
            status=CalibrationStatus.INCOMPLETE,
            reason="insufficient_comparable_measurements",
            budget_seconds=total_budget_seconds,
            sample=sample,
            candidates=candidates,
            measurements=tuple(measurements),
            tournament=None,
            selection=None,
        )

    tournament = select_measured_candidate(
        candidates=candidates,
        measurements=tournament_measurements,
        minimum_gain_fraction=settings.calibration_min_gain_fraction,
    )
    winner = _candidate_for_key(candidates, tournament.winner)
    selection = (
        MeasuredResourceSelection(
            candidate=winner,
            confidence=tournament.confidence,
            reason="calibration_measurement",
            evidence_count=len(completed_keys),
        )
        if winner is not None
        else None
    )
    status = CalibrationStatus.COMPLETED if selection is not None else CalibrationStatus.INCOMPLETE
    return CalibrationExecutionResult(
        status=status,
        reason=tournament.reason.value,
        budget_seconds=total_budget_seconds,
        sample=sample,
        candidates=candidates,
        measurements=tuple(measurements),
        tournament=tournament,
        selection=selection,
    )


def _remeasure_finalists(
    *,
    plan: TranscodePlan,
    candidates: tuple[PerformanceCandidate, ...],
    sample: BenchmarkSample,
    measurements: list[CalibrationMeasurementResult],
    calibration_dir: Path,
    runner: CalibrationProcessRunner,
    command_renderer: Av1anCommandRenderer,
    cancellation_token: ProcessCancellationToken,
    warmup_seconds: float,
    total_budget_seconds: float,
    started_at: float,
    monotonic: Callable[[], float],
    status_sink: CalibrationStatusSink | None,
) -> None:
    candidate_by_key = {candidate_key(candidate): candidate for candidate in candidates}
    for round_number in range(1, MAX_FINALIST_ROUNDS + 1):
        if cancellation_token.cancel_requested:
            return
        remaining_budget = _remaining_budget(
            total_budget_seconds=total_budget_seconds,
            started_at=started_at,
            monotonic=monotonic,
        )
        completed_keys = _completed_candidate_keys(measurements)
        finalists = next_measurement_candidates(
            candidates=candidates,
            measurements=tuple(_tournament_measurement(item, sample) for item in measurements),
            remaining_budget_seconds=remaining_budget,
            close_fraction=FINALIST_CLOSE_FRACTION,
        )
        finalists = tuple(
            key
            for key in reversed(finalists)
            if key in candidate_by_key and key in completed_keys
        )
        if not finalists or not _can_complete_finalist_round(
            finalists=finalists,
            measurements=measurements,
            remaining_budget_seconds=remaining_budget,
        ):
            return
        for index, key in enumerate(finalists, start=1):
            remaining_budget = _remaining_budget(
                total_budget_seconds=total_budget_seconds,
                started_at=started_at,
                monotonic=monotonic,
            )
            timeout_seconds = remaining_budget - CALIBRATION_DEADLINE_GUARD_SECONDS
            if timeout_seconds <= 0:
                return
            candidate = candidate_by_key[key]
            if status_sink is not None:
                status_sink(
                    f"calibrating finalist {index}/{len(finalists)} "
                    f"round {round_number}/{MAX_FINALIST_ROUNDS} "
                    f"(workers={candidate.workers}, lp={candidate.svt_lp})"
                )
            result = measure_candidate(
                plan=plan,
                candidate=candidate,
                sample=sample,
                calibration_dir=calibration_dir,
                runner=runner,
                command_renderer=command_renderer,
                cancellation_token=cancellation_token,
                warmup_seconds=warmup_seconds,
                timeout_seconds=timeout_seconds,
            )
            measurements.append(result)
            _discard_candidate_payload(result)
            if status_sink is not None:
                status_sink(
                    f"calibration finalist {index}/{len(finalists)} "
                    f"round {round_number}/{MAX_FINALIST_ROUNDS} {result.status.value}"
                )
            if result.status is not CalibrationMeasurementStatus.COMPLETED:
                return


def _remaining_budget(
    *,
    total_budget_seconds: float,
    started_at: float,
    monotonic: Callable[[], float],
) -> float:
    return max(0.0, total_budget_seconds - (monotonic() - started_at))


def _can_complete_finalist_round(
    *,
    finalists: tuple[CandidateKey, ...],
    measurements: list[CalibrationMeasurementResult],
    remaining_budget_seconds: float,
) -> bool:
    completed_costs = [
        item.elapsed_seconds
        for item in measurements
        if item.status is CalibrationMeasurementStatus.COMPLETED
        and item.elapsed_seconds > 0
    ]
    if not completed_costs:
        return False
    startup_adjusted = completed_costs[1:] or completed_costs
    estimated_measurement_seconds = median(startup_adjusted)
    estimated_round_seconds = estimated_measurement_seconds * len(finalists)
    guard = CALIBRATION_DEADLINE_GUARD_SECONDS * len(finalists)
    return remaining_budget_seconds >= estimated_round_seconds + guard


def _completed_candidate_keys(
    measurements: list[CalibrationMeasurementResult],
) -> set[CandidateKey]:
    return {
        item.candidate_key
        for item in measurements
        if item.status is CalibrationMeasurementStatus.COMPLETED
    }


def _completed_measurement_keys(
    measurements: tuple[CandidateMeasurement, ...],
) -> set[CandidateKey]:
    return {
        item.candidate_key
        for item in measurements
        if not item.failed and not item.resource_pressure
    }


def _tournament_measurement(
    result: CalibrationMeasurementResult,
    sample: BenchmarkSample,
) -> CandidateMeasurement:
    observation_duration = result.metrics.observation_duration_seconds
    aggregate_fps = result.metrics.aggregate_fps
    if (
        observation_duration is not None
        and observation_duration > 0
        and aggregate_fps is not None
        and aggregate_fps > 0
    ):
        frames = aggregate_fps * observation_duration
        duration_seconds = observation_duration
    else:
        frames = float(sample.estimated_frames)
        duration_seconds = result.elapsed_seconds
    return CandidateMeasurement(
        candidate_key=result.candidate_key,
        frames=frames,
        duration_seconds=duration_seconds,
        failed=result.status
        not in {
            CalibrationMeasurementStatus.COMPLETED,
            CalibrationMeasurementStatus.RESOURCE_PRESSURE,
        },
        resource_pressure=result.status is CalibrationMeasurementStatus.RESOURCE_PRESSURE,
    )


def _candidate_for_key(
    candidates: tuple[PerformanceCandidate, ...],
    key: CandidateKey | None,
) -> PerformanceCandidate | None:
    if key is None:
        return None
    return next((candidate for candidate in candidates if candidate_key(candidate) == key), None)


def _discard_candidate_payload(result: CalibrationMeasurementResult) -> None:
    result.output_path.unlink(missing_ok=True)
    if result.temp_dir.exists():
        shutil.rmtree(result.temp_dir, ignore_errors=True)


def _skipped(*, reason: str, budget_seconds: float) -> CalibrationExecutionResult:
    return CalibrationExecutionResult(
        status=CalibrationStatus.SKIPPED,
        reason=reason,
        budget_seconds=budget_seconds,
        sample=None,
        candidates=(),
        measurements=(),
        tournament=None,
        selection=None,
    )
