from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlmodel import Session

from avarch.adapters.scheduler_run import SqliteSchedulerRunStore
from avarch.adapters.sqlite.db import create_db_engine, create_db_schema
from avarch.adapters.sqlite.models import (
    Job,
    JobAttempt,
    MediaFile,
    MediaFileStatus,
    PerformanceObservation,
)
from avarch.application.environment_signature import build_execution_environment_signature
from avarch.application.resources import ResourceConfidence, ResourceSnapshot, ResourceValue
from avarch.application.workload_signature import build_workload_signature
from avarch.config import AppConfig, DatabaseSettings
from avarch.domain.jobs import AttemptStatus, JobStage, JobStatus, ResourceClass
from avarch.domain.scheduler import (
    ActiveJob,
    ClaimableJob,
    JobResourceReservation,
    ResourceCapacity,
    select_launchable_jobs,
)
from avarch.models.plan import TranscodePlan
from avarch.serialization import canonical_json
from tests.test_plan_models import sample_plan


def test_reserved_heavy_jobs_launch_when_cpu_and_memory_fit() -> None:
    selected = select_launchable_jobs(
        [
            ClaimableJob(
                job_id=1,
                stage=JobStage.ENCODE,
                reservation=JobResourceReservation(cpu=2, memory_bytes=2 * 1024**3),
            ),
            ClaimableJob(
                job_id=2,
                stage=JobStage.ENCODE,
                reservation=JobResourceReservation(cpu=2, memory_bytes=2 * 1024**3),
            ),
        ],
        active_jobs=[],
        capacity=ResourceCapacity(
            cheap_workers=1,
            av1an_jobs=2,
            file_ops=1,
            cpu_budget=4,
            memory_budget_bytes=4 * 1024**3,
        ),
    )

    assert [job.job_id for job in selected] == [1, 2]


def test_reserved_heavy_jobs_do_not_exceed_cpu_or_memory_budget() -> None:
    capacity = ResourceCapacity(
        cheap_workers=1,
        av1an_jobs=3,
        file_ops=1,
        cpu_budget=4,
        memory_budget_bytes=4 * 1024**3,
    )

    cpu_blocked = select_launchable_jobs(
        [
            ClaimableJob(
                job_id=1,
                stage=JobStage.ENCODE,
                reservation=JobResourceReservation(cpu=3, memory_bytes=1 * 1024**3),
            ),
            ClaimableJob(
                job_id=2,
                stage=JobStage.ENCODE,
                reservation=JobResourceReservation(cpu=2, memory_bytes=1 * 1024**3),
            ),
        ],
        active_jobs=[],
        capacity=capacity,
    )
    memory_blocked = select_launchable_jobs(
        [
            ClaimableJob(
                job_id=1,
                stage=JobStage.ENCODE,
                reservation=JobResourceReservation(cpu=1, memory_bytes=3 * 1024**3),
            ),
            ClaimableJob(
                job_id=2,
                stage=JobStage.ENCODE,
                reservation=JobResourceReservation(cpu=1, memory_bytes=2 * 1024**3),
            ),
        ],
        active_jobs=[],
        capacity=capacity,
    )

    assert [job.job_id for job in cpu_blocked] == [1]
    assert [job.job_id for job in memory_blocked] == [1]


def test_unknown_exclusive_heavy_demand_blocks_other_heavy_jobs() -> None:
    selected = select_launchable_jobs(
        [
            ClaimableJob(
                job_id=1,
                stage=JobStage.ENCODE,
                reservation=JobResourceReservation(exclusive=True),
            ),
            ClaimableJob(
                job_id=2,
                stage=JobStage.ENCODE,
                reservation=JobResourceReservation(cpu=1, memory_bytes=1),
            ),
        ],
        active_jobs=[],
        capacity=ResourceCapacity(cheap_workers=1, av1an_jobs=2, file_ops=1),
    )

    assert [job.job_id for job in selected] == [1]


def test_existing_count_capacity_still_applies_to_non_heavy_jobs() -> None:
    selected = select_launchable_jobs(
        [
            ClaimableJob(job_id=1, stage=JobStage.VALIDATE),
            ClaimableJob(job_id=2, stage=JobStage.PROBE),
            ClaimableJob(job_id=3, stage=JobStage.PROMOTE),
        ],
        active_jobs=[ActiveJob(job_id=10, stage=JobStage.PROMOTE)],
        capacity=ResourceCapacity(cheap_workers=1, av1an_jobs=2, file_ops=1),
    )

    assert [job.job_id for job in selected] == [1]


def test_reservation_rejects_negative_arithmetic() -> None:
    with pytest.raises(ValueError):
        JobResourceReservation(cpu=-1)
    with pytest.raises(ValueError):
        JobResourceReservation(memory_bytes=-1)


def test_sqlite_scheduler_store_builds_bounded_reservations_from_history(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(cpu_count=6, memory_bytes=8 * 1024**3)
    monkeypatch.setattr(
        "avarch.adapters.scheduler_run.effective_resource_snapshot",
        lambda: snapshot,
    )
    config = _stored_parallel_encode_jobs(tmp_path, snapshot=snapshot, peak_memory=2 * 1024**3)

    claimable = SqliteSchedulerRunStore(config).claimable_jobs(active_job_ids=set())
    selected = select_launchable_jobs(
        claimable,
        active_jobs=[],
        capacity=ResourceCapacity(
            cheap_workers=1,
            av1an_jobs=2,
            file_ops=1,
            cpu_budget=4,
            memory_budget_bytes=6 * 1024**3,
        ),
    )

    assert len(claimable) == 2
    assert all(job.reservation is not None for job in claimable)
    assert all(job.reservation is not None and not job.reservation.exclusive for job in claimable)
    assert [job.job_id for job in selected] == [1, 2]


def test_sqlite_scheduler_store_blocks_second_job_when_history_memory_would_exceed_budget(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(cpu_count=6, memory_bytes=8 * 1024**3)
    monkeypatch.setattr(
        "avarch.adapters.scheduler_run.effective_resource_snapshot",
        lambda: snapshot,
    )
    config = _stored_parallel_encode_jobs(tmp_path, snapshot=snapshot, peak_memory=3 * 1024**3)

    claimable = SqliteSchedulerRunStore(config).claimable_jobs(active_job_ids=set())
    selected = select_launchable_jobs(
        claimable,
        active_jobs=[],
        capacity=ResourceCapacity(
            cheap_workers=1,
            av1an_jobs=2,
            file_ops=1,
            cpu_budget=4,
            memory_budget_bytes=6 * 1024**3,
        ),
    )

    assert [job.job_id for job in selected] == [1]


def _stored_parallel_encode_jobs(
    tmp_path: Path,
    *,
    snapshot: ResourceSnapshot,
    peak_memory: int,
) -> AppConfig:
    database_url = f"sqlite:///{tmp_path / 'avarch.adapters.sqlite.db'}"
    engine = create_db_engine(database_url)
    create_db_schema(engine)
    plan = _bounded_plan(tmp_path, plan_hash="plan-1")
    second_plan = _bounded_plan(tmp_path, plan_hash="plan-2")
    history_plan = _bounded_plan(tmp_path, plan_hash="history-plan")
    now = datetime(2026, 7, 1, 12, tzinfo=UTC)
    with Session(engine) as session:
        _write_plan(plan)
        _write_plan(second_plan)
        _insert_queued_encode(session, job_id=1, plan=plan, now=now)
        _insert_queued_encode(session, job_id=2, plan=second_plan, now=now)
        history_job_id = _insert_queued_encode(session, job_id=3, plan=history_plan, now=now)
        attempt = JobAttempt(
            job_id=history_job_id,
            attempt_number=1,
            stage=JobStage.ENCODE,
            resource_class=ResourceClass.HEAVY_AV1AN,
            status=AttemptStatus.COMPLETED,
            runner_id="history",
            started_at=now,
            finished_at=now,
        )
        session.add(attempt)
        session.flush()
        assert attempt.id is not None
        env = build_execution_environment_signature(
            snapshot=snapshot,
            tool_versions={
                "av1an_version_family": history_plan.execution_identity.av1an_version_family,
                "vapoursynth_version": history_plan.vapoursynth.vapoursynth_version,
            },
        )
        workload = build_workload_signature(history_plan)
        session.add(
            PerformanceObservation(
                schema_version=1,
                job_id=history_job_id,
                attempt_id=attempt.id,
                plan_hash=history_plan.plan_hash,
                semantic_hash=history_plan.semantic_hash,
                resource_policy_hash=history_plan.resource_policy_hash,
                environment_signature_hash=env.signature_hash,
                environment_signature_json=canonical_json(env.to_payload()),
                workload_signature_hash=workload.signature_hash,
                workload_signature_json=canonical_json(workload.to_payload()),
                total_frames=1200,
                observation_duration_seconds=60.0,
                aggregate_fps=20.0,
                peak_rss_bytes=peak_memory,
                peak_cgroup_memory_bytes=peak_memory,
                average_cpu_utilization_percent=80.0,
                swap_current_bytes_delta=0,
                cpu_throttled_events_delta=0,
                cpu_throttled_usec_delta=0,
                memory_oom_events_delta=0,
                memory_oom_kill_events_delta=0,
                resource_attribution_available=True,
                incomplete=False,
                progress_samples_observed=2,
                resource_samples_observed=1,
                warmup_seconds=0.0,
                created_at=now,
            )
        )
        history_job = session.get(Job, history_job_id)
        assert history_job is not None
        history_job.status = JobStatus.PROMOTED
        history_job.stage = JobStage.PROMOTE
        history_job.finished_at = now
        session.add(history_job)
        session.commit()
    return AppConfig(database=DatabaseSettings(url=database_url))


def _bounded_plan(tmp_path: Path, *, plan_hash: str) -> TranscodePlan:
    artifact_dir = tmp_path / plan_hash
    artifact_dir.mkdir()
    base = sample_plan().model_copy(
        update={
            "plan_hash": plan_hash,
            "semantic_hash": "semantic",
            "resource_policy_hash": "resource-policy",
        }
    )
    return base.model_copy(
        update={
            "av1an": base.av1an.model_copy(
                update={
                    "workers": 1,
                    "encoder_args": ["--preset", "6", "--crf", "28", "--lp", "2"],
                }
            ),
            "artifacts": base.artifacts.model_copy(
                update={"plan_json": artifact_dir / "plan.json"}
            ),
        }
    )


def _write_plan(plan: TranscodePlan) -> None:
    plan.artifacts.plan_json.write_text(canonical_json(plan), encoding="utf-8")


def _insert_queued_encode(
    session: Session,
    *,
    job_id: int,
    plan: TranscodePlan,
    now: datetime,
) -> int:
    media_file = MediaFile(
        id=job_id,
        path=f"/media/{job_id}.mkv",
        size_bytes=123,
        mtime_ns=456,
        device_id=789,
        inode=1000 + job_id,
        fs_fingerprint=f"source-fs-{job_id}",
        discovered_at=now,
        last_seen_at=now,
        status=MediaFileStatus.PRESENT,
    )
    session.add(media_file)
    session.flush()
    session.add(
        Job(
            id=job_id,
            media_file_id=job_id,
            profile_name=plan.profile_name,
            profile_hash=plan.profile_hash,
            source_fs_fingerprint=media_file.fs_fingerprint,
            queue_key=f"queue-{job_id}",
            probe_hash=plan.probe_hash,
            plan_hash=plan.plan_hash,
            plan_path=str(plan.artifacts.plan_json),
            output_path=str(plan.output_path),
            status=JobStatus.QUEUED,
            stage=JobStage.ENCODE,
            priority=0,
            attempts=0,
            created_at=now,
            updated_at=now,
        )
    )
    return job_id


def _snapshot(*, cpu_count: int, memory_bytes: int) -> ResourceSnapshot:
    return ResourceSnapshot(
        effective_cpu_count=cpu_count,
        effective_cpu_quota=float(cpu_count),
        effective_memory_bytes=memory_bytes,
        cpu_values=(ResourceValue("cgroup_v2", float(cpu_count), ResourceConfidence.HIGH),),
        memory_values=(ResourceValue("cgroup_v2", memory_bytes, ResourceConfidence.HIGH),),
    )
