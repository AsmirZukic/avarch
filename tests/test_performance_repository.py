from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlmodel import Session

from avarch.adapters.sqlite.db import create_db_engine, create_db_schema
from avarch.adapters.sqlite.models import (
    CalibrationObservation,
    Job,
    JobAttempt,
    MediaFile,
    MediaFileStatus,
    PerformanceObservation,
)
from avarch.adapters.sqlite.performance import (
    CALIBRATION_OBSERVATION_SCHEMA_VERSION,
    CALIBRATION_STATUS_COMPLETED,
    PerformanceObservationPersistenceError,
    calibration_observation_for_key,
    compatible_calibration_observations,
    compatible_performance_observations,
    observation_for_attempt,
    persist_calibration_observation,
    persist_performance_observation,
)
from avarch.application.attempt_metrics import (
    ATTEMPT_METRICS_SCHEMA_VERSION,
    AttemptMetricsSummary,
)
from avarch.application.environment_signature import build_execution_environment_signature
from avarch.application.resources import ResourceConfidence, ResourceSnapshot, ResourceValue
from avarch.application.workload_signature import build_workload_signature
from avarch.domain.jobs import AttemptStatus, JobStage, JobStatus, ResourceClass
from tests.test_plan_models import sample_plan


def test_persist_performance_observation_links_attempt_and_metrics(tmp_path: Path) -> None:
    engine, job_id, attempt_id = _stored_encode_attempt(tmp_path)
    now = datetime(2026, 7, 1, 12, tzinfo=UTC)

    with Session(engine) as session:
        job = session.get(Job, job_id)
        attempt = session.get(JobAttempt, attempt_id)
        assert job is not None
        assert attempt is not None

        observation = persist_performance_observation(
            session,
            job=job,
            attempt=attempt,
            metrics=_summary(),
            created_at=now,
            semantic_hash="semantic-hash",
            resource_policy_hash="resource-policy-hash",
            resource_decision={"mode": "manual", "effective_workers": 4},
            tool_versions={"av1an": "0.5.1", "svt_av1": "3.0.2"},
            environment_signature=build_execution_environment_signature(
                snapshot=_resource_snapshot(),
                tool_versions={"av1an": "0.5.1", "svt_av1": "3.0.2"},
            ),
            workload_signature=build_workload_signature(
                sample_plan().model_copy(update={"semantic_hash": "semantic-hash"})
            ),
        )
        session.commit()
        observation_id = observation.id

    with Session(engine) as session:
        stored = observation_for_attempt(session, attempt_id=attempt_id)

    assert stored is not None
    assert stored.id == observation_id
    assert stored.job_id == job_id
    assert stored.attempt_id == attempt_id
    assert stored.schema_version == ATTEMPT_METRICS_SCHEMA_VERSION
    assert stored.plan_hash == "plan-hash"
    assert stored.semantic_hash == "semantic-hash"
    assert stored.resource_policy_hash == "resource-policy-hash"
    assert json.loads(stored.resource_decision_json or "{}") == {
        "effective_workers": 4,
        "mode": "manual",
    }
    assert json.loads(stored.tool_versions_json or "{}") == {
        "av1an": "0.5.1",
        "svt_av1": "3.0.2",
    }
    assert stored.environment_signature_hash is not None
    assert json.loads(stored.environment_signature_json or "{}")[
        "signature_hash"
    ] == stored.environment_signature_hash
    assert stored.workload_signature_hash is not None
    assert json.loads(stored.workload_signature_json or "{}")[
        "signature_hash"
    ] == stored.workload_signature_hash
    assert stored.total_frames == 1200
    assert stored.observation_duration_seconds == 60.0
    assert stored.aggregate_fps == 20.0
    assert stored.peak_rss_bytes == 1024
    assert stored.peak_cgroup_memory_bytes == 2048
    assert stored.average_cpu_utilization_percent == 85.5
    assert stored.swap_current_bytes_delta == 0
    assert stored.cpu_throttled_events_delta == 2
    assert stored.cpu_throttled_usec_delta == 300
    assert stored.memory_oom_events_delta == 0
    assert stored.memory_oom_kill_events_delta == 0
    assert stored.resource_attribution_available is True
    assert stored.incomplete is False
    assert stored.progress_samples_observed == 3
    assert stored.resource_samples_observed == 2
    assert stored.warmup_seconds == 10.0


def test_persist_performance_observation_is_idempotent_per_attempt(tmp_path: Path) -> None:
    engine, job_id, attempt_id = _stored_encode_attempt(tmp_path)
    now = datetime(2026, 7, 1, 12, tzinfo=UTC)

    with Session(engine) as session:
        job = session.get(Job, job_id)
        attempt = session.get(JobAttempt, attempt_id)
        assert job is not None
        assert attempt is not None
        first = persist_performance_observation(
            session,
            job=job,
            attempt=attempt,
            metrics=_summary(total_frames=1200),
            created_at=now,
        )
        second = persist_performance_observation(
            session,
            job=job,
            attempt=attempt,
            metrics=_summary(total_frames=2400),
            created_at=now,
        )
        first_id = first.id
        second_id = second.id
        session.commit()

    assert second_id == first_id
    with Session(engine) as session:
        stored = observation_for_attempt(session, attempt_id=attempt_id)

    assert stored is not None
    assert stored.total_frames == 1200


def test_persist_performance_observation_rejects_unpersisted_job(
    tmp_path: Path,
) -> None:
    engine, _job_id, attempt_id = _stored_encode_attempt(tmp_path)
    with Session(engine) as session:
        attempt = session.get(JobAttempt, attempt_id)
        assert attempt is not None
        transient_job = Job(
            media_file_id=1,
            profile_name="av1_1080p_sdr",
            profile_hash="profile-hash",
            source_fs_fingerprint="source-fs",
            queue_key="transient",
            status=JobStatus.ENCODING,
            stage=JobStage.ENCODE,
            priority=0,
            attempts=1,
            created_at=datetime(2026, 7, 1, 12, tzinfo=UTC),
            updated_at=datetime(2026, 7, 1, 12, tzinfo=UTC),
        )

        with pytest.raises(PerformanceObservationPersistenceError):
            persist_performance_observation(
                session,
                job=transient_job,
                attempt=attempt,
                metrics=_summary(),
                created_at=datetime(2026, 7, 1, 12, tzinfo=UTC),
            )


def test_compatible_observations_rank_exact_matches_before_semantic_matches(
    tmp_path: Path,
) -> None:
    engine, job_id, attempt_id = _stored_encode_attempt(tmp_path)
    now = datetime(2026, 7, 1, 12, tzinfo=UTC)
    with Session(engine) as session:
        _add_observation(
            session,
            job_id=job_id,
            attempt_id=attempt_id,
            environment_hash="env",
            workload_hash="semantic-only-workload",
            semantic_hash="semantic",
            fps=30.0,
            created_at=now,
        )
        _add_observation(
            session,
            job_id=job_id,
            attempt_id=_add_attempt(session, job_id=job_id, number=2),
            environment_hash="env",
            workload_hash="workload",
            semantic_hash="semantic",
            fps=24.0,
            created_at=now,
        )
        session.commit()

    with Session(engine) as session:
        observations = compatible_performance_observations(
            session,
            environment_signature_hash="env",
            workload_signature_hash="workload",
            semantic_hash="semantic",
            limit=10,
        )

    assert [observation.match_quality for observation in observations] == ["exact", "semantic"]
    assert [observation.aggregate_fps for observation in observations] == [24.0, 30.0]


def test_compatible_observations_exclude_incompatible_or_non_positive_evidence(
    tmp_path: Path,
) -> None:
    engine, job_id, attempt_id = _stored_encode_attempt(tmp_path)
    now = datetime(2026, 7, 1, 12, tzinfo=UTC)
    with Session(engine) as session:
        _add_observation(
            session,
            job_id=job_id,
            attempt_id=attempt_id,
            environment_hash="env",
            workload_hash="workload",
            semantic_hash="semantic",
            fps=24.0,
            created_at=now,
        )
        _add_observation(
            session,
            job_id=job_id,
            attempt_id=_add_attempt(session, job_id=job_id, number=2),
            environment_hash="other-env",
            workload_hash="workload",
            semantic_hash="semantic",
            fps=99.0,
            created_at=now,
        )
        _add_observation(
            session,
            job_id=job_id,
            attempt_id=_add_attempt(session, job_id=job_id, number=3),
            environment_hash="env",
            workload_hash="workload",
            semantic_hash="semantic",
            fps=99.0,
            created_at=now,
            incomplete=True,
        )
        _add_observation(
            session,
            job_id=job_id,
            attempt_id=_add_attempt(session, job_id=job_id, number=4),
            environment_hash="env",
            workload_hash="workload",
            semantic_hash="semantic",
            fps=99.0,
            created_at=now,
            cpu_throttled_events_delta=1,
        )
        session.commit()

    with Session(engine) as session:
        observations = compatible_performance_observations(
            session,
            environment_signature_hash="env",
            workload_signature_hash="workload",
            semantic_hash="semantic",
            limit=10,
        )

    assert [observation.aggregate_fps for observation in observations] == [24.0]


def test_compatible_observations_are_deterministic_under_ties(tmp_path: Path) -> None:
    engine, job_id, attempt_id = _stored_encode_attempt(tmp_path)
    now = datetime(2026, 7, 1, 12, tzinfo=UTC)
    with Session(engine) as session:
        _add_observation(
            session,
            job_id=job_id,
            attempt_id=attempt_id,
            environment_hash="env",
            workload_hash="workload",
            semantic_hash="semantic",
            fps=24.0,
            created_at=now,
        )
        later_attempt_id = _add_attempt(session, job_id=job_id, number=2)
        _add_observation(
            session,
            job_id=job_id,
            attempt_id=later_attempt_id,
            environment_hash="env",
            workload_hash="workload",
            semantic_hash="semantic",
            fps=24.0,
            created_at=now,
        )
        session.commit()

    with Session(engine) as session:
        observations = compatible_performance_observations(
            session,
            environment_signature_hash="env",
            workload_signature_hash="workload",
            semantic_hash="semantic",
            limit=1,
        )

    assert [observation.attempt_id for observation in observations] == [later_attempt_id]


def test_persist_calibration_observation_records_context_and_payload(
    tmp_path: Path,
) -> None:
    engine, _job_id, _attempt_id = _stored_encode_attempt(tmp_path)
    now = datetime(2026, 7, 1, 12, tzinfo=UTC)
    environment = build_execution_environment_signature(
        snapshot=_resource_snapshot(),
        tool_versions={"av1an": "0.5.1"},
    )
    workload = build_workload_signature(sample_plan())

    with Session(engine) as session:
        observation = persist_calibration_observation(
            session,
            calibration_key="calibration-key",
            environment_signature=environment,
            workload_signature=workload,
            semantic_hash="semantic",
            resource_policy_hash="resource-policy",
            sample={"start_seconds": 120, "duration_seconds": 30},
            candidates=({"workers": "auto", "svt_lp": "native"}, {"workers": 2, "svt_lp": 4}),
            measurements=({"workers": 2, "svt_lp": 4, "fps": 30.0},),
            winner={"workers": 2, "svt_lp": 4},
            status=CALIBRATION_STATUS_COMPLETED,
            confidence=0.8,
            measurement_cost_seconds=45.0,
            created_at=now,
        )
        session.commit()
        observation_id = observation.id

    with Session(engine) as session:
        stored = calibration_observation_for_key(
            session,
            calibration_key="calibration-key",
        )

    assert stored is not None
    assert stored.id == observation_id
    assert stored.schema_version == CALIBRATION_OBSERVATION_SCHEMA_VERSION
    assert stored.environment_signature_hash == environment.signature_hash
    assert stored.workload_signature_hash == workload.signature_hash
    assert json.loads(stored.sample_json) == {"duration_seconds": 30, "start_seconds": 120}
    assert json.loads(stored.winner_json or "{}") == {"svt_lp": 4, "workers": 2}
    assert stored.confidence == 0.8
    assert stored.measurement_cost_seconds == 45.0


def test_persist_calibration_observation_is_idempotent_by_key(tmp_path: Path) -> None:
    engine, _job_id, _attempt_id = _stored_encode_attempt(tmp_path)
    now = datetime(2026, 7, 1, 12, tzinfo=UTC)
    environment = build_execution_environment_signature(
        snapshot=_resource_snapshot(),
        tool_versions={"av1an": "0.5.1"},
    )
    workload = build_workload_signature(sample_plan())

    with Session(engine) as session:
        first = persist_calibration_observation(
            session,
            calibration_key="calibration-key",
            environment_signature=environment,
            workload_signature=workload,
            sample={"start_seconds": 120},
            candidates=(),
            measurements=(),
            status=CALIBRATION_STATUS_COMPLETED,
            confidence=0.8,
            measurement_cost_seconds=45.0,
            created_at=now,
        )
        second = persist_calibration_observation(
            session,
            calibration_key="calibration-key",
            environment_signature=environment,
            workload_signature=workload,
            sample={"start_seconds": 240},
            candidates=(),
            measurements=(),
            status=CALIBRATION_STATUS_COMPLETED,
            confidence=0.1,
            measurement_cost_seconds=5.0,
            created_at=now,
        )
        first_id = first.id
        second_id = second.id
        session.commit()

    assert second_id == first_id
    with Session(engine) as session:
        stored = calibration_observation_for_key(
            session,
            calibration_key="calibration-key",
        )

    assert stored is not None
    assert json.loads(stored.sample_json) == {"start_seconds": 120}
    assert stored.confidence == 0.8


def test_stronger_calibration_replaces_weaker_row_for_same_key(tmp_path: Path) -> None:
    engine, _job_id, _attempt_id = _stored_encode_attempt(tmp_path)
    now = datetime(2026, 7, 1, 12, tzinfo=UTC)
    environment = build_execution_environment_signature(
        snapshot=_resource_snapshot(),
        tool_versions={"av1an": "0.5.1"},
    )
    workload = build_workload_signature(sample_plan())

    with Session(engine) as session:
        first = persist_calibration_observation(
            session,
            calibration_key="calibration-key",
            environment_signature=environment,
            workload_signature=workload,
            sample={"start_seconds": 120},
            candidates=(),
            measurements=(),
            winner={"workers": "auto", "svt_lp": "native"},
            status=CALIBRATION_STATUS_COMPLETED,
            confidence=0.5,
            measurement_cost_seconds=10.0,
            created_at=now,
        )
        first_id = first.id
        stronger = persist_calibration_observation(
            session,
            calibration_key="calibration-key",
            environment_signature=environment,
            workload_signature=workload,
            sample={"start_seconds": 240},
            candidates=(),
            measurements=(),
            winner={"workers": 4, "svt_lp": 4},
            status=CALIBRATION_STATUS_COMPLETED,
            confidence=0.8,
            measurement_cost_seconds=12.0,
            created_at=now + timedelta(minutes=1),
        )
        stronger_id = stronger.id
        session.commit()

    assert stronger_id == first_id
    with Session(engine) as session:
        stored = calibration_observation_for_key(session, calibration_key="calibration-key")
    assert stored is not None
    assert stored.confidence == 0.8
    assert json.loads(stored.winner_json or "{}") == {"svt_lp": 4, "workers": 4}


def test_compatible_calibrations_exclude_incompatible_or_partial_evidence(
    tmp_path: Path,
) -> None:
    engine, _job_id, _attempt_id = _stored_encode_attempt(tmp_path)
    now = datetime(2026, 7, 1, 12, tzinfo=UTC)
    environment = build_execution_environment_signature(
        snapshot=_resource_snapshot(),
        tool_versions={"av1an": "0.5.1"},
    )
    other_environment = build_execution_environment_signature(
        snapshot=_resource_snapshot(cpu_count=2),
        tool_versions={"av1an": "0.5.1"},
    )
    workload = build_workload_signature(sample_plan())
    other_workload = build_workload_signature(
        sample_plan().model_copy(
            update={
                "video": sample_plan().video.model_copy(update={"target_width": 1280}),
            }
        )
    )

    with Session(engine) as session:
        _add_calibration(
            session,
            key="usable",
            environment_hash=environment.signature_hash,
            workload_hash=workload.signature_hash,
            semantic_hash="semantic",
            created_at=now,
        )
        _add_calibration(
            session,
            key="other-env",
            environment_hash=other_environment.signature_hash,
            workload_hash=workload.signature_hash,
            semantic_hash="semantic",
            created_at=now,
        )
        _add_calibration(
            session,
            key="partial",
            environment_hash=environment.signature_hash,
            workload_hash=workload.signature_hash,
            semantic_hash="semantic",
            created_at=now,
            incomplete=True,
        )
        _add_calibration(
            session,
            key="cancelled",
            environment_hash=environment.signature_hash,
            workload_hash=workload.signature_hash,
            semantic_hash="semantic",
            created_at=now,
            status="cancelled",
        )
        _add_calibration(
            session,
            key="semantic-compatible",
            environment_hash=environment.signature_hash,
            workload_hash=other_workload.signature_hash,
            semantic_hash="semantic",
            created_at=now,
        )
        session.commit()

    with Session(engine) as session:
        observations = compatible_calibration_observations(
            session,
            environment_signature_hash=environment.signature_hash,
            workload_signature_hash=workload.signature_hash,
            semantic_hash="semantic",
            limit=10,
        )

    assert [observation.calibration_key for observation in observations] == [
        "semantic-compatible",
        "usable",
    ]


def _stored_encode_attempt(tmp_path: Path) -> tuple[Engine, int, int]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'avarch.adapters.sqlite.db'}")
    create_db_schema(engine)
    now = datetime(2026, 7, 1, 11, tzinfo=UTC)
    with Session(engine) as session:
        media_file = MediaFile(
            path="/media/performance.mkv",
            size_bytes=123,
            mtime_ns=456,
            device_id=789,
            inode=101112,
            fs_fingerprint="source-fs",
            discovered_at=now,
            last_seen_at=now,
            status=MediaFileStatus.PRESENT,
        )
        session.add(media_file)
        session.flush()
        assert media_file.id is not None
        job = Job(
            media_file_id=media_file.id,
            profile_name="av1_1080p_sdr",
            profile_hash="profile-hash",
            source_fs_fingerprint=media_file.fs_fingerprint,
            queue_key="queue-performance",
            probe_hash="probe-hash",
            plan_hash="plan-hash",
            plan_path="/work/plan.json",
            output_path="/work/output.mkv",
            status=JobStatus.ENCODING,
            stage=JobStage.ENCODE,
            priority=0,
            attempts=1,
            created_at=now,
            updated_at=now,
        )
        session.add(job)
        session.flush()
        assert job.id is not None
        attempt = JobAttempt(
            job_id=job.id,
            attempt_number=1,
            stage=JobStage.ENCODE,
            resource_class=ResourceClass.HEAVY_AV1AN,
            status=AttemptStatus.COMPLETED,
            runner_id="runner",
            started_at=now,
            finished_at=now,
        )
        session.add(attempt)
        session.commit()
        session.refresh(job)
        session.refresh(attempt)
        assert job.id is not None
        assert attempt.id is not None
        return engine, job.id, attempt.id


def _add_attempt(session: Session, *, job_id: int, number: int) -> int:
    now = datetime(2026, 7, 1, 11, tzinfo=UTC)
    attempt = JobAttempt(
        job_id=job_id,
        attempt_number=number,
        stage=JobStage.ENCODE,
        resource_class=ResourceClass.HEAVY_AV1AN,
        status=AttemptStatus.COMPLETED,
        runner_id=f"runner-{number}",
        started_at=now,
        finished_at=now,
    )
    session.add(attempt)
    session.flush()
    assert attempt.id is not None
    return attempt.id


def _add_observation(
    session: Session,
    *,
    job_id: int,
    attempt_id: int,
    environment_hash: str,
    workload_hash: str,
    semantic_hash: str,
    fps: float,
    created_at: datetime,
    incomplete: bool = False,
    cpu_throttled_events_delta: int = 0,
) -> None:
    session.add(
        PerformanceObservation(
            schema_version=ATTEMPT_METRICS_SCHEMA_VERSION,
            job_id=job_id,
            attempt_id=attempt_id,
            plan_hash="plan-hash",
            semantic_hash=semantic_hash,
            resource_policy_hash="resource-policy-hash",
            resource_decision_json=None,
            tool_versions_json=None,
            environment_signature_hash=environment_hash,
            environment_signature_json=None,
            workload_signature_hash=workload_hash,
            workload_signature_json=None,
            total_frames=1200,
            observation_duration_seconds=60.0,
            aggregate_fps=fps,
            peak_rss_bytes=1024,
            peak_cgroup_memory_bytes=2048,
            average_cpu_utilization_percent=80.0,
            swap_current_bytes_delta=0,
            cpu_throttled_events_delta=cpu_throttled_events_delta,
            cpu_throttled_usec_delta=0,
            memory_oom_events_delta=0,
            memory_oom_kill_events_delta=0,
            resource_attribution_available=True,
            incomplete=incomplete,
            progress_samples_observed=2,
            resource_samples_observed=0,
            warmup_seconds=0.0,
            created_at=created_at,
        )
    )


def _add_calibration(
    session: Session,
    *,
    key: str,
    environment_hash: str,
    workload_hash: str,
    semantic_hash: str,
    created_at: datetime,
    status: str = CALIBRATION_STATUS_COMPLETED,
    incomplete: bool = False,
) -> None:
    session.add(
        CalibrationObservation(
            schema_version=CALIBRATION_OBSERVATION_SCHEMA_VERSION,
            calibration_key=key,
            semantic_hash=semantic_hash,
            resource_policy_hash="resource-policy-hash",
            environment_signature_hash=environment_hash,
            environment_signature_json=None,
            workload_signature_hash=workload_hash,
            workload_signature_json=None,
            sample_json="{}",
            candidates_json="[]",
            measurements_json="[]",
            winner_json="{}",
            status=status,
            confidence=0.8,
            measurement_cost_seconds=30.0,
            incomplete=incomplete,
            created_at=created_at,
        )
    )


def _summary(*, total_frames: int = 1200) -> AttemptMetricsSummary:
    return AttemptMetricsSummary(
        schema_version=ATTEMPT_METRICS_SCHEMA_VERSION,
        total_frames=total_frames,
        observation_duration_seconds=60.0,
        aggregate_fps=20.0,
        peak_rss_bytes=1024,
        peak_cgroup_memory_bytes=2048,
        average_cpu_utilization_percent=85.5,
        swap_current_bytes_delta=0,
        cpu_throttled_events_delta=2,
        cpu_throttled_usec_delta=300,
        memory_oom_events_delta=0,
        memory_oom_kill_events_delta=0,
        resource_attribution_available=True,
        incomplete=False,
        progress_samples_observed=3,
        resource_samples_observed=2,
        warmup_seconds=10.0,
    )


def _resource_snapshot(*, cpu_count: int = 4) -> ResourceSnapshot:
    return ResourceSnapshot(
        effective_cpu_count=cpu_count,
        effective_cpu_quota=float(cpu_count),
        effective_memory_bytes=8 * 1024**3,
        cpu_values=(ResourceValue("cgroup_v2", float(cpu_count), ResourceConfidence.HIGH),),
        memory_values=(ResourceValue("cgroup_v2", 8 * 1024**3, ResourceConfidence.HIGH),),
    )
