from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine
from sqlmodel import Session

from avarch.adapters.sqlite.db import create_db_engine, create_db_schema
from avarch.adapters.sqlite.job_views import SqliteJobViewStore
from avarch.adapters.sqlite.models import (
    Job,
    JobAttempt,
    MediaFile,
    MediaFileStatus,
    PerformanceObservation,
)
from avarch.adapters.sqlite.progress import SqliteProgressStore
from avarch.application.job_views import current_job_progress
from avarch.domain.jobs import AttemptStatus, JobStage, JobStatus, ResourceClass
from avarch.domain.progress import ProgressPhase, ProgressSnapshot, ProgressSource, ProgressUnit
from avarch.serialization import canonical_json


def test_current_job_progress_for_queued_job_with_no_attempts(tmp_path: Path) -> None:
    engine, job_id = _stored_job(tmp_path, status=JobStatus.QUEUED, stage=JobStage.ENCODE)

    with Session(engine) as session:
        view = current_job_progress(SqliteJobViewStore(session), job_id=job_id)

    assert view is not None
    assert view.job_id == job_id
    assert view.job_status == JobStatus.QUEUED
    assert view.job_stage == JobStage.ENCODE
    assert view.attempt is None
    assert view.progress is None


def test_current_job_progress_for_running_job_with_progress(tmp_path: Path) -> None:
    engine, job_id = _stored_job(tmp_path, status=JobStatus.ENCODING, stage=JobStage.ENCODE)
    attempt_id = _add_attempt(engine, job_id=job_id, number=1, status=AttemptStatus.RUNNING)
    observed = datetime(2026, 7, 1, 12, tzinfo=UTC)
    _save_progress(engine, attempt_id=attempt_id, snapshot=_snapshot(observed, current=42))

    with Session(engine) as session:
        view = current_job_progress(SqliteJobViewStore(session), job_id=job_id)

    assert view is not None
    assert view.attempt is not None
    assert view.attempt.attempt_number == 1
    assert view.attempt.status == AttemptStatus.RUNNING
    assert view.progress is not None
    assert view.progress.phase == ProgressPhase.ENCODING
    assert view.progress.current == 42.0


def test_current_job_progress_includes_resource_decision_metadata(tmp_path: Path) -> None:
    engine, job_id = _stored_job(tmp_path, status=JobStatus.ENCODING, stage=JobStage.ENCODE)
    _add_attempt(
        engine,
        job_id=job_id,
        number=1,
        status=AttemptStatus.RUNNING,
        command_json=canonical_json(
            {
                "resource_decision": {
                    "schema_version": 1,
                    "mode": "native",
                    "effective_workers": "auto",
                    "effective_svt_lp": "native",
                    "reason": "insufficient_evidence",
                    "confidence": 0.0,
                    "algorithm_version": 1,
                    "fallback": True,
                    "evidence_count": 3,
                }
            }
        ),
    )

    with Session(engine) as session:
        view = current_job_progress(SqliteJobViewStore(session), job_id=job_id)

    assert view is not None
    assert view.attempt is not None
    assert view.attempt.resource_decision is not None
    assert view.attempt.resource_decision.mode == "native"
    assert view.attempt.resource_decision.effective_workers == "auto"
    assert view.attempt.resource_decision.fallback is True
    assert view.attempt.resource_decision.evidence_count == 3


def test_current_job_progress_ignores_legacy_command_without_resource_decision(
    tmp_path: Path,
) -> None:
    engine, job_id = _stored_job(tmp_path, status=JobStatus.ENCODING, stage=JobStage.ENCODE)
    _add_attempt(
        engine,
        job_id=job_id,
        number=1,
        status=AttemptStatus.RUNNING,
        command_json=canonical_json({"av1an_argv": ["av1an", "--workers", "4"]}),
    )

    with Session(engine) as session:
        view = current_job_progress(SqliteJobViewStore(session), job_id=job_id)

    assert view is not None
    assert view.attempt is not None
    assert view.attempt.resource_decision is None


def test_job_details_include_performance_observation(tmp_path: Path) -> None:
    engine, job_id = _stored_job(tmp_path, status=JobStatus.ENCODING, stage=JobStage.ENCODE)
    attempt_id = _add_attempt(engine, job_id=job_id, number=1, status=AttemptStatus.COMPLETED)
    _add_performance_observation(engine, job_id=job_id, attempt_id=attempt_id)

    with Session(engine) as session:
        details = SqliteJobViewStore(session).job_details(job_id=job_id)

    assert details is not None
    assert len(details.attempt_history) == 1
    performance = details.attempt_history[0].performance
    assert performance is not None
    assert performance.schema_version == 1
    assert performance.aggregate_fps == 24.0
    assert performance.peak_cgroup_memory_bytes == 3 * 1024 * 1024
    assert performance.average_cpu_utilization_percent == 75.0
    assert performance.cpu_throttled_events_delta == 2
    assert performance.memory_oom_kill_events_delta == 0
    assert performance.incomplete is False
    assert performance.resource_policy_hash == "resource-policy-hash"


def test_current_job_progress_for_completed_job_keeps_final_progress(
    tmp_path: Path,
) -> None:
    engine, job_id = _stored_job(tmp_path, status=JobStatus.PROMOTED, stage=JobStage.PROMOTE)
    attempt_id = _add_attempt(engine, job_id=job_id, number=1, status=AttemptStatus.COMPLETED)
    observed = datetime(2026, 7, 1, 12, tzinfo=UTC)
    _save_progress(
        engine,
        attempt_id=attempt_id,
        snapshot=_snapshot(observed, phase=ProgressPhase.COMPLETED, current=100),
    )

    with Session(engine) as session:
        view = current_job_progress(SqliteJobViewStore(session), job_id=job_id)

    assert view is not None
    assert view.attempt is not None
    assert view.attempt.status == AttemptStatus.COMPLETED
    assert view.progress is not None
    assert view.progress.phase == ProgressPhase.COMPLETED


def test_current_job_progress_for_failed_latest_attempt(tmp_path: Path) -> None:
    engine, job_id = _stored_job(tmp_path, status=JobStatus.FAILED, stage=JobStage.ENCODE)
    attempt_id = _add_attempt(engine, job_id=job_id, number=1, status=AttemptStatus.FAILED)
    observed = datetime(2026, 7, 1, 12, tzinfo=UTC)
    _save_progress(
        engine,
        attempt_id=attempt_id,
        snapshot=_snapshot(observed, phase=ProgressPhase.FAILED, current=18),
    )

    with Session(engine) as session:
        view = current_job_progress(SqliteJobViewStore(session), job_id=job_id)

    assert view is not None
    assert view.attempt is not None
    assert view.attempt.status == AttemptStatus.FAILED
    assert view.progress is not None
    assert view.progress.phase == ProgressPhase.FAILED


def test_current_job_progress_uses_latest_retry_attempt(tmp_path: Path) -> None:
    engine, job_id = _stored_job(tmp_path, status=JobStatus.ENCODING, stage=JobStage.ENCODE)
    first_attempt_id = _add_attempt(
        engine,
        job_id=job_id,
        number=1,
        status=AttemptStatus.FAILED,
    )
    second_attempt_id = _add_attempt(
        engine,
        job_id=job_id,
        number=2,
        status=AttemptStatus.RUNNING,
    )
    observed = datetime(2026, 7, 1, 12, tzinfo=UTC)
    _save_progress(engine, attempt_id=first_attempt_id, snapshot=_snapshot(observed, current=90))
    _save_progress(
        engine,
        attempt_id=second_attempt_id,
        snapshot=_snapshot(observed + timedelta(seconds=1), current=5),
    )

    with Session(engine) as session:
        view = current_job_progress(SqliteJobViewStore(session), job_id=job_id)

    assert view is not None
    assert view.attempt is not None
    assert view.attempt.attempt_number == 2
    assert view.progress is not None
    assert view.progress.current == 5.0


def test_current_job_progress_uses_latest_attempt_even_without_progress(
    tmp_path: Path,
) -> None:
    engine, job_id = _stored_job(tmp_path, status=JobStatus.ENCODING, stage=JobStage.ENCODE)
    first_attempt_id = _add_attempt(
        engine,
        job_id=job_id,
        number=1,
        status=AttemptStatus.FAILED,
    )
    _add_attempt(engine, job_id=job_id, number=2, status=AttemptStatus.RUNNING)
    observed = datetime(2026, 7, 1, 12, tzinfo=UTC)
    _save_progress(engine, attempt_id=first_attempt_id, snapshot=_snapshot(observed, current=90))

    with Session(engine) as session:
        view = current_job_progress(SqliteJobViewStore(session), job_id=job_id)

    assert view is not None
    assert view.attempt is not None
    assert view.attempt.attempt_number == 2
    assert view.progress is None


def test_current_job_progress_returns_none_for_missing_job(tmp_path: Path) -> None:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'avarch.adapters.sqlite.db'}")
    create_db_schema(engine)

    with Session(engine) as session:
        view = current_job_progress(SqliteJobViewStore(session), job_id=999)

    assert view is None


def _stored_job(
    tmp_path: Path,
    *,
    status: JobStatus,
    stage: JobStage,
) -> tuple[Engine, int]:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'avarch.adapters.sqlite.db'}")
    create_db_schema(engine)
    now = datetime(2026, 7, 1, 11, tzinfo=UTC)
    with Session(engine) as session:
        media_file = MediaFile(
            path="/media/progress-view.mkv",
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
        media_file_id = media_file.id
        assert media_file_id is not None

        job = Job(
            media_file_id=media_file_id,
            profile_name="av1_1080p_sdr",
            profile_hash="profile-hash",
            source_fs_fingerprint=media_file.fs_fingerprint,
            queue_key="queue-progress-view",
            status=status,
            stage=stage,
            priority=0,
            attempts=0,
            created_at=now,
            updated_at=now,
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        job_id = job.id
        assert job_id is not None
        return engine, job_id


def _add_attempt(
    engine: Engine,
    *,
    job_id: int,
    number: int,
    status: AttemptStatus,
    command_json: str | None = None,
) -> int:
    now = datetime(2026, 7, 1, 11, tzinfo=UTC) + timedelta(seconds=number)
    with Session(engine) as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.attempts = max(job.attempts, number)
        attempt = JobAttempt(
            job_id=job_id,
            attempt_number=number,
            stage=job.stage,
            resource_class=ResourceClass.HEAVY_AV1AN,
            status=status,
            runner_id=f"runner-{number}",
            started_at=now,
            finished_at=now if status != AttemptStatus.RUNNING else None,
            command_json=command_json,
        )
        session.add(job)
        session.add(attempt)
        session.commit()
        session.refresh(attempt)
        attempt_id = attempt.id
        assert attempt_id is not None
        return attempt_id


def _save_progress(
    engine: Engine,
    *,
    attempt_id: int,
    snapshot: ProgressSnapshot,
) -> None:
    with Session(engine) as session:
        SqliteProgressStore(session).save_snapshot(
            attempt_id=attempt_id,
            snapshot=snapshot,
            persisted_at=snapshot.observed_at,
        )
        session.commit()


def _add_performance_observation(
    engine: Engine,
    *,
    job_id: int,
    attempt_id: int,
) -> None:
    with Session(engine) as session:
        session.add(
            PerformanceObservation(
                schema_version=1,
                job_id=job_id,
                attempt_id=attempt_id,
                plan_hash="plan-hash",
                semantic_hash="semantic-hash",
                resource_policy_hash="resource-policy-hash",
                resource_decision_json=None,
                tool_versions_json=None,
                total_frames=1200,
                observation_duration_seconds=50.0,
                aggregate_fps=24.0,
                peak_rss_bytes=2 * 1024 * 1024,
                peak_cgroup_memory_bytes=3 * 1024 * 1024,
                average_cpu_utilization_percent=75.0,
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
                created_at=datetime(2026, 7, 1, 12, tzinfo=UTC),
            )
        )
        session.commit()


def _snapshot(
    observed_at: datetime,
    *,
    phase: ProgressPhase = ProgressPhase.ENCODING,
    current: float = 10,
) -> ProgressSnapshot:
    return ProgressSnapshot(
        phase=phase,
        current=current,
        total=100,
        unit=ProgressUnit.FRAMES,
        rate_per_second=2.0,
        speed_ratio=1.5,
        source=ProgressSource.SCHEDULER,
        message=None,
        phase_started_at=observed_at,
        observed_at=observed_at,
        heartbeat_at=observed_at,
        advanced_at=observed_at,
    )
