from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from sqlmodel import Session, col, select

from avarch.adapters.sqlite.models import (
    CalibrationObservation,
    Job,
    JobAttempt,
    PerformanceObservation,
)
from avarch.application.attempt_metrics import AttemptMetricsSummary
from avarch.application.environment_signature import ExecutionEnvironmentSignature
from avarch.application.workload_signature import WorkloadSignature
from avarch.serialization import canonical_json

CALIBRATION_OBSERVATION_SCHEMA_VERSION = 1
CALIBRATION_STATUS_COMPLETED = "completed"


class PerformanceObservationPersistenceError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CompatiblePerformanceObservation:
    id: int
    job_id: int
    attempt_id: int
    aggregate_fps: float
    peak_memory_bytes: int | None
    match_quality: str
    resource_decision_json: str | None
    workload_signature_hash: str | None
    semantic_hash: str | None
    resource_policy_hash: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class CompatibleCalibrationObservation:
    id: int
    calibration_key: str
    confidence: float
    measurement_cost_seconds: float
    winner_json: str | None
    created_at: datetime


def persist_performance_observation(
    session: Session,
    *,
    job: Job,
    attempt: JobAttempt,
    metrics: AttemptMetricsSummary,
    created_at: datetime,
    plan_hash: str | None = None,
    semantic_hash: str | None = None,
    resource_policy_hash: str | None = None,
    resource_decision: Mapping[str, object] | None = None,
    tool_versions: Mapping[str, object] | None = None,
    environment_signature: ExecutionEnvironmentSignature | None = None,
    workload_signature: WorkloadSignature | None = None,
) -> PerformanceObservation:
    if job.id is None or attempt.id is None:
        raise PerformanceObservationPersistenceError("Job and attempt must be persisted.")
    if attempt.job_id != job.id:
        raise PerformanceObservationPersistenceError("Attempt does not belong to job.")

    existing = observation_for_attempt(session, attempt_id=attempt.id)
    if existing is not None:
        return existing

    observation = PerformanceObservation(
        schema_version=metrics.schema_version,
        job_id=job.id,
        attempt_id=attempt.id,
        plan_hash=plan_hash if plan_hash is not None else job.plan_hash,
        semantic_hash=semantic_hash,
        resource_policy_hash=resource_policy_hash,
        resource_decision_json=(
            canonical_json(dict(resource_decision)) if resource_decision is not None else None
        ),
        tool_versions_json=(
            canonical_json(dict(tool_versions)) if tool_versions is not None else None
        ),
        environment_signature_hash=(
            environment_signature.signature_hash if environment_signature is not None else None
        ),
        environment_signature_json=(
            canonical_json(environment_signature.to_payload())
            if environment_signature is not None
            else None
        ),
        workload_signature_hash=(
            workload_signature.signature_hash if workload_signature is not None else None
        ),
        workload_signature_json=(
            canonical_json(workload_signature.to_payload())
            if workload_signature is not None
            else None
        ),
        total_frames=metrics.total_frames,
        observation_duration_seconds=metrics.observation_duration_seconds,
        aggregate_fps=metrics.aggregate_fps,
        peak_rss_bytes=metrics.peak_rss_bytes,
        peak_cgroup_memory_bytes=metrics.peak_cgroup_memory_bytes,
        average_cpu_utilization_percent=metrics.average_cpu_utilization_percent,
        swap_current_bytes_delta=metrics.swap_current_bytes_delta,
        cpu_throttled_events_delta=metrics.cpu_throttled_events_delta,
        cpu_throttled_usec_delta=metrics.cpu_throttled_usec_delta,
        memory_oom_events_delta=metrics.memory_oom_events_delta,
        memory_oom_kill_events_delta=metrics.memory_oom_kill_events_delta,
        resource_attribution_available=metrics.resource_attribution_available,
        incomplete=metrics.incomplete,
        progress_samples_observed=metrics.progress_samples_observed,
        resource_samples_observed=metrics.resource_samples_observed,
        warmup_seconds=metrics.warmup_seconds,
        created_at=created_at,
    )
    session.add(observation)
    session.flush()
    if observation.id is None:
        raise PerformanceObservationPersistenceError("Performance observation id was not assigned.")
    return observation


def persist_calibration_observation(
    session: Session,
    *,
    calibration_key: str,
    environment_signature: ExecutionEnvironmentSignature,
    workload_signature: WorkloadSignature,
    sample: Mapping[str, object],
    candidates: tuple[Mapping[str, object], ...],
    measurements: tuple[Mapping[str, object], ...],
    status: str,
    confidence: float,
    measurement_cost_seconds: float,
    created_at: datetime,
    semantic_hash: str | None = None,
    resource_policy_hash: str | None = None,
    winner: Mapping[str, object] | None = None,
    incomplete: bool = False,
) -> CalibrationObservation:
    existing = calibration_observation_for_key(session, calibration_key=calibration_key)
    if existing is not None:
        should_replace = existing.incomplete or (
            not incomplete and confidence > existing.confidence
        )
        if should_replace:
            existing.sample_json = canonical_json(dict(sample))
            existing.candidates_json = canonical_json([dict(candidate) for candidate in candidates])
            existing.measurements_json = canonical_json(
                [dict(measurement) for measurement in measurements]
            )
            existing.winner_json = canonical_json(dict(winner)) if winner is not None else None
            existing.status = status
            existing.confidence = confidence
            existing.measurement_cost_seconds = measurement_cost_seconds
            existing.incomplete = incomplete
            existing.created_at = created_at
            session.add(existing)
            session.flush()
        return existing
    observation = CalibrationObservation(
        schema_version=CALIBRATION_OBSERVATION_SCHEMA_VERSION,
        calibration_key=calibration_key,
        semantic_hash=semantic_hash,
        resource_policy_hash=resource_policy_hash,
        environment_signature_hash=environment_signature.signature_hash,
        environment_signature_json=canonical_json(environment_signature.to_payload()),
        workload_signature_hash=workload_signature.signature_hash,
        workload_signature_json=canonical_json(workload_signature.to_payload()),
        sample_json=canonical_json(dict(sample)),
        candidates_json=canonical_json([dict(candidate) for candidate in candidates]),
        measurements_json=canonical_json([dict(measurement) for measurement in measurements]),
        winner_json=canonical_json(dict(winner)) if winner is not None else None,
        status=status,
        confidence=confidence,
        measurement_cost_seconds=measurement_cost_seconds,
        incomplete=incomplete,
        created_at=created_at,
    )
    session.add(observation)
    session.flush()
    if observation.id is None:
        raise PerformanceObservationPersistenceError("Calibration observation id was not assigned.")
    return observation


def calibration_observation_for_key(
    session: Session,
    *,
    calibration_key: str,
) -> CalibrationObservation | None:
    return session.exec(
        select(CalibrationObservation).where(
            CalibrationObservation.calibration_key == calibration_key
        )
    ).first()


def compatible_calibration_observations(
    session: Session,
    *,
    environment_signature_hash: str,
    workload_signature_hash: str,
    semantic_hash: str | None,
    limit: int,
) -> list[CompatibleCalibrationObservation]:
    if limit <= 0:
        return []
    rows = list(
        session.exec(
            select(CalibrationObservation)
            .where(CalibrationObservation.environment_signature_hash == environment_signature_hash)
            .where(CalibrationObservation.incomplete == False)  # noqa: E712
            .where(CalibrationObservation.status == CALIBRATION_STATUS_COMPLETED)
            .order_by(
                col(CalibrationObservation.created_at).desc(),
                col(CalibrationObservation.id).desc(),
            )
        ).all()
    )
    compatible = [
        _compatible_calibration(row)
        for row in rows
        if row.workload_signature_hash == workload_signature_hash
        or (semantic_hash is not None and row.semantic_hash == semantic_hash)
    ]
    return compatible[:limit]


def observation_for_attempt(
    session: Session,
    *,
    attempt_id: int,
) -> PerformanceObservation | None:
    return session.exec(
        select(PerformanceObservation).where(PerformanceObservation.attempt_id == attempt_id)
    ).first()


def compatible_performance_observations(
    session: Session,
    *,
    environment_signature_hash: str,
    workload_signature_hash: str,
    semantic_hash: str | None,
    limit: int,
) -> list[CompatiblePerformanceObservation]:
    if limit <= 0:
        return []
    rows = list(
        session.exec(
            select(PerformanceObservation)
            .where(PerformanceObservation.environment_signature_hash == environment_signature_hash)
            .where(PerformanceObservation.incomplete == False)  # noqa: E712
            .order_by(
                col(PerformanceObservation.created_at).desc(),
                col(PerformanceObservation.id).desc(),
            )
        ).all()
    )
    evidence = [
        _compatible_observation(row, workload_signature_hash=workload_signature_hash)
        for row in rows
        if _is_positive_observation(row)
        and _is_compatible(
            row,
            workload_signature_hash=workload_signature_hash,
            semantic_hash=semantic_hash,
        )
    ]
    evidence.sort(key=_evidence_sort_key)
    return evidence[:limit]


def _compatible_observation(
    row: PerformanceObservation,
    *,
    workload_signature_hash: str,
) -> CompatiblePerformanceObservation:
    row_id = row.id
    if row_id is None:
        raise PerformanceObservationPersistenceError("Performance observation id is missing.")
    aggregate_fps = row.aggregate_fps
    if aggregate_fps is None:
        raise PerformanceObservationPersistenceError("Compatible observation is missing FPS.")
    return CompatiblePerformanceObservation(
        id=row_id,
        job_id=row.job_id,
        attempt_id=row.attempt_id,
        aggregate_fps=aggregate_fps,
        peak_memory_bytes=row.peak_cgroup_memory_bytes or row.peak_rss_bytes,
        match_quality="exact"
        if row.workload_signature_hash == workload_signature_hash
        else "semantic",
        resource_decision_json=row.resource_decision_json,
        workload_signature_hash=row.workload_signature_hash,
        semantic_hash=row.semantic_hash,
        resource_policy_hash=row.resource_policy_hash,
        created_at=row.created_at,
    )


def _is_positive_observation(row: PerformanceObservation) -> bool:
    return (
        row.aggregate_fps is not None
        and not row.incomplete
        and (row.memory_oom_events_delta or 0) == 0
        and (row.memory_oom_kill_events_delta or 0) == 0
        and (row.cpu_throttled_events_delta or 0) == 0
    )


def _is_compatible(
    row: PerformanceObservation,
    *,
    workload_signature_hash: str,
    semantic_hash: str | None,
) -> bool:
    if row.workload_signature_hash == workload_signature_hash:
        return True
    return semantic_hash is not None and row.semantic_hash == semantic_hash


def _evidence_sort_key(row: CompatiblePerformanceObservation) -> tuple[int, float, int]:
    quality = 0 if row.match_quality == "exact" else 1
    return (quality, -row.created_at.timestamp(), -row.id)


def _compatible_calibration(row: CalibrationObservation) -> CompatibleCalibrationObservation:
    row_id = row.id
    if row_id is None:
        raise PerformanceObservationPersistenceError("Calibration observation id is missing.")
    return CompatibleCalibrationObservation(
        id=row_id,
        calibration_key=row.calibration_key,
        confidence=row.confidence,
        measurement_cost_seconds=row.measurement_cost_seconds,
        winner_json=row.winner_json,
        created_at=row.created_at,
    )
