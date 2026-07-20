from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session, col, select

from avarch.adapters.scheduler_run import SqliteSchedulerRunStore
from avarch.adapters.scheduler_workers import (
    execute_cleanup_job,
    execute_encode_job,
    execute_promotion_job,
    execute_validation_job,
)
from avarch.adapters.sqlite.db import create_db_engine, create_db_schema
from avarch.adapters.sqlite.models import (
    Job,
    JobAttempt,
    JobAttemptProgress,
    JobEvent,
    MediaFile,
    MediaFileStatus,
    PerformanceObservation,
)
from avarch.adapters.sqlite.queue import claimable_jobs
from avarch.adapters.validation import ValidationExecutionError
from avarch.application.environment_signature import build_execution_environment_signature
from avarch.application.progress import ProgressSink
from avarch.application.promotion import (
    PromotionPreflightView,
    PromotionRecordView,
    PromotionResultView,
)
from avarch.application.resource_decision import RESOURCE_DECISION_ALGORITHM_VERSION
from avarch.application.resources import ResourceConfidence, ResourceSnapshot, ResourceValue
from avarch.application.workload_signature import build_workload_signature
from avarch.config import AppConfig, DatabaseSettings
from avarch.domain.jobs import AttemptStatus, JobEventType, JobStage, JobStatus, ResourceClass
from avarch.domain.progress import ProgressPhase, ProgressSnapshot, ProgressSource, ProgressUnit
from avarch.domain.scheduler import ResourceCapacity, has_resource_capacity
from avarch.models.promotion import PromotionMode, PromotionStatus
from avarch.models.validation import (
    ObservedValidationMedia,
    ValidationCheck,
    ValidationCheckStatus,
    ValidationReport,
)
from avarch.serialization import canonical_json


def test_job_a_promotes_while_job_b_is_encoding(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        media_file = _media_file(tmp_path / "movie-a.mkv", now)
        session.add(media_file)
        session.flush()
        promoting = _job(
            media_file.id or 0,
            now,
            queue_key="promote",
            status=JobStatus.READY_TO_PROMOTE,
            stage=JobStage.PROMOTE,
        )
        session.add(promoting)

    with Session(engine) as session:
        claimable = claimable_jobs(session, active_job_ids={2})

    assert [job.stage for job in claimable] == [JobStage.PROMOTE]
    assert has_resource_capacity(
        claimable[0].stage,
        [JobStage.ENCODE],
        capacity=ResourceCapacity(cheap_workers=4, av1an_jobs=1, file_ops=1),
    )


def test_job_a_validates_while_job_b_is_encoding(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        media_file = _media_file(tmp_path / "movie-a.mkv", now)
        session.add(media_file)
        session.flush()
        session.add(
            _job(
                media_file.id or 0,
                now,
                queue_key="validate",
                status=JobStatus.ENCODED,
                stage=JobStage.VALIDATE,
            )
        )

    with Session(engine) as session:
        claimable = claimable_jobs(session, active_job_ids={2})

    assert [job.stage for job in claimable] == [JobStage.VALIDATE]
    assert has_resource_capacity(
        claimable[0].stage,
        [JobStage.ENCODE],
        capacity=ResourceCapacity(cheap_workers=4, av1an_jobs=1, file_ops=1),
    )


def test_cleanup_can_run_while_another_job_encodes(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        media_file = _media_file(tmp_path / "movie-a.mkv", now)
        session.add(media_file)
        session.flush()
        session.add(
            _job(
                media_file.id or 0,
                now,
                queue_key="cleanup",
                status=JobStatus.QUEUED,
                stage=JobStage.CLEANUP,
            )
        )

    with Session(engine) as session:
        claimable = claimable_jobs(session, active_job_ids={2})

    assert [job.stage for job in claimable] == [JobStage.CLEANUP]
    assert has_resource_capacity(
        claimable[0].stage,
        [JobStage.ENCODE],
        capacity=ResourceCapacity(cheap_workers=4, av1an_jobs=1, file_ops=1),
    )


def test_failed_validation_does_not_block_next_job(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        media_file = _media_file(tmp_path / "movie-a.mkv", now)
        session.add(media_file)
        session.flush()
        session.add(
            _job(
                media_file.id or 0,
                now,
                queue_key="failed-validation",
                status=JobStatus.VALIDATION_FAILED,
                stage=JobStage.VALIDATE,
            )
        )
        session.add(_job(media_file.id or 0, now, queue_key="next"))

    with Session(engine) as session:
        claimable = claimable_jobs(session, active_job_ids=set())

    assert [job.queue_key for job in claimable] == ["next"]


def test_size_rejection_does_not_block_next_job(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        media_file = _media_file(tmp_path / "movie-a.mkv", now)
        session.add(media_file)
        session.flush()
        session.add(
            _job(
                media_file.id or 0,
                now,
                queue_key="size-rejected",
                status=JobStatus.SIZE_REJECTED,
                stage=JobStage.VALIDATE,
            )
        )
        session.add(_job(media_file.id or 0, now, queue_key="next"))

    with Session(engine) as session:
        claimable = claimable_jobs(session, active_job_ids=set())

    assert [job.queue_key for job in claimable] == ["next"]


def test_scheduler_continues_after_promotion_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fake_promote_job(**_kwargs: object) -> PromotionResultView:
        return PromotionResultView(
            job_id=1,
            promotion_id=0,
            status=PromotionStatus.FAILED,
            final_path=Path(),
            promoted=False,
            error_message="failed",
        )

    monkeypatch.setattr("avarch.adapters.scheduler_workers.promote_job", fake_promote_job)
    database_path = tmp_path / "avarch.adapters.sqlite.db"
    config = AppConfig(database=DatabaseSettings(url=f"sqlite:///{database_path}"))
    engine = create_db_engine(config.database.url)
    create_db_schema(engine)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        media_file = _media_file(tmp_path / "movie-a.mkv", now)
        session.add(media_file)
        session.flush()
        session.add(
            _job(
                media_file.id or 0,
                now,
                queue_key="promote",
                status=JobStatus.READY_TO_PROMOTE,
                stage=JobStage.PROMOTE,
            )
        )

    asyncio.run(
        execute_promotion_job(
            job_id=1,
            runner_id="runner",
            config=config,
            promotion_workflow=_PromotionWorkflowStub(),
        )
    )


def test_encode_worker_requests_process_token_when_cancelled(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from avarch.models.execution import ExecutionInterruptedError, ProcessCancellationToken
    from tests.test_execution import _sample_plan  # pyright: ignore[reportPrivateUsage]

    database_path = tmp_path / "avarch.adapters.sqlite.db"
    config = AppConfig(database=DatabaseSettings(url=f"sqlite:///{database_path}"))
    engine = create_db_engine(config.database.url)
    create_db_schema(engine)
    plan = _sample_plan(tmp_path)
    plan.artifacts.plan_json.parent.mkdir(parents=True, exist_ok=True)
    plan.artifacts.plan_json.write_text(canonical_json(plan), encoding="utf-8")
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        media_file = MediaFile(
            path=str(plan.input_path),
            size_bytes=plan.input_path.stat().st_size,
            mtime_ns=plan.input_path.stat().st_mtime_ns,
            device_id=plan.input_path.stat().st_dev,
            inode=plan.input_path.stat().st_ino,
            fs_fingerprint=plan.source_fs_fingerprint,
            discovered_at=now,
            last_seen_at=now,
            status=MediaFileStatus.PRESENT,
        )
        session.add(media_file)
        session.flush()
        session.add(
            Job(
                media_file_id=media_file.id or 0,
                profile_name=plan.profile_name,
                profile_hash=plan.profile_hash,
                source_fs_fingerprint=plan.source_fs_fingerprint,
                queue_key="encode",
                plan_hash=plan.plan_hash,
                plan_path=str(plan.artifacts.plan_json),
                status=JobStatus.QUEUED,
                stage=JobStage.ENCODE,
                created_at=now,
                updated_at=now,
            )
        )

    started = threading.Event()
    observed_token: dict[str, ProcessCancellationToken | None] = {"token": None}

    def fake_execute_plan(
        _plan: object,
        *,
        cancellation_token: ProcessCancellationToken | None = None,
        progress_sink: object | None = None,
    ) -> object:
        del progress_sink
        observed_token["token"] = cancellation_token
        started.set()
        deadline = time.monotonic() + 2.0
        while cancellation_token is not None and not cancellation_token.cancel_requested:
            if time.monotonic() > deadline:
                raise AssertionError("worker did not request process cancellation")
            time.sleep(0.02)
        raise ExecutionInterruptedError("cancelled")

    monkeypatch.setattr("avarch.adapters.scheduler_workers.execute_plan", fake_execute_plan)

    async def scenario() -> None:
        task = asyncio.create_task(
            execute_encode_job(job_id=1, runner_id="runner", config=config)
        )
        deadline = time.monotonic() + 1.0
        while not started.is_set():
            if time.monotonic() > deadline:
                raise AssertionError("worker did not start fake encode")
            await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.wait_for(task, timeout=3.0)

    asyncio.run(scenario())

    token = observed_token["token"]
    assert token is not None
    assert token.cancel_requested is True
    with Session(engine) as session:
        progress = session.get(JobAttemptProgress, 1)
    assert progress is not None
    assert ProgressPhase(progress.phase) == ProgressPhase.CANCELLED


def test_encode_worker_records_failed_progress_on_execution_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from avarch.models.execution import ExecutionError

    config, _plan = _stored_encode_job(tmp_path)

    def fake_execute_plan(
        _plan: object,
        *,
        cancellation_token: object | None = None,
        progress_sink: ProgressSink | None = None,
    ) -> object:
        del cancellation_token, progress_sink
        raise ExecutionError("encoder failed\nfull diagnostics stay in logs")

    monkeypatch.setattr("avarch.adapters.scheduler_workers.execute_plan", fake_execute_plan)

    asyncio.run(execute_encode_job(job_id=1, runner_id="runner", config=config))

    engine = create_db_engine(config.database.url)
    with Session(engine) as session:
        progress = session.get(JobAttemptProgress, 1)

    assert progress is not None
    assert ProgressPhase(progress.phase) == ProgressPhase.FAILED
    assert progress.message == "encoder failed full diagnostics stay in logs"
    assert len(progress.message) <= 500


def test_encode_worker_persists_resource_decision_metadata(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, _plan = _stored_encode_job(tmp_path)
    monkeypatch.setattr(
        "avarch.adapters.scheduler_workers.effective_resource_snapshot",
        lambda: ResourceSnapshot(
            effective_cpu_count=4,
            effective_cpu_quota=4.0,
            effective_memory_bytes=8 * 1024**3,
            cpu_values=(ResourceValue("cgroup_v2", 4.0, ResourceConfidence.HIGH),),
            memory_values=(
                ResourceValue("cgroup_v2", 8 * 1024**3, ResourceConfidence.HIGH),
            ),
        ),
    )

    def fake_execute_plan(
        _plan: object,
        *,
        cancellation_token: object | None = None,
        progress_sink: ProgressSink | None = None,
    ) -> object:
        del cancellation_token, progress_sink
        return "completed"

    monkeypatch.setattr("avarch.adapters.scheduler_workers.execute_plan", fake_execute_plan)

    asyncio.run(execute_encode_job(job_id=1, runner_id="runner", config=config))

    engine = create_db_engine(config.database.url)
    with Session(engine) as session:
        attempt = session.get(JobAttempt, 1)

    assert attempt is not None
    assert attempt.command_json is not None
    command = json.loads(attempt.command_json)
    assert command["resource_decision"] == {
        "algorithm_version": 2,
        "confidence": 1.0,
        "effective_svt_lp": "native",
        "effective_workers": 2,
        "evidence_count": None,
        "fallback": False,
        "mode": "manual",
        "reason": "manual_override",
        "schema_version": 1,
    }
    assert command["resource_snapshot"]["effective_cpu_count"] == 4
    assert command["resource_snapshot"]["effective_memory_bytes"] == 8 * 1024**3


def test_encode_worker_uses_compatible_history_for_auto_resource_selection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, plan = _stored_encode_job(tmp_path)
    config = config.model_copy(
        update={"resources": config.resources.model_copy(update={"cpu_reserve": 0.0})}
    )
    plan = plan.model_copy(
        update={
            "av1an": plan.av1an.model_copy(
                update={
                    "workers": "auto",
                    "encoder_args": ["--preset", "6", "--crf", "28"],
                }
            ),
            "semantic_hash": "semantic-history",
            "resource_policy_hash": "resource-policy-history",
        }
    )
    plan.artifacts.plan_json.write_text(canonical_json(plan), encoding="utf-8")
    snapshot = ResourceSnapshot(
        effective_cpu_count=8,
        effective_cpu_quota=8.0,
        effective_memory_bytes=16 * 1024**3,
        cpu_values=(ResourceValue("cgroup_v2", 8.0, ResourceConfidence.HIGH),),
        memory_values=(ResourceValue("cgroup_v2", 16 * 1024**3, ResourceConfidence.HIGH),),
    )
    monkeypatch.setattr(
        "avarch.adapters.scheduler_workers.effective_resource_snapshot",
        lambda: snapshot,
    )
    _add_resource_history(config=config, plan=plan, snapshot=snapshot)

    def fake_execute_plan(
        selected_plan: Any,
        *,
        cancellation_token: object | None = None,
        progress_sink: ProgressSink | None = None,
    ) -> object:
        del cancellation_token, progress_sink
        assert selected_plan.av1an.workers == 4
        assert selected_plan.av1an.encoder_args[-2:] == ["--lp", "2"]
        return "completed"

    monkeypatch.setattr("avarch.adapters.scheduler_workers.execute_plan", fake_execute_plan)

    asyncio.run(execute_encode_job(job_id=1, runner_id="runner", config=config))

    engine = create_db_engine(config.database.url)
    with Session(engine) as session:
        attempt = session.exec(
            select(JobAttempt).where(JobAttempt.runner_id == "runner")
        ).one()

    assert attempt.command_json is not None
    command = json.loads(attempt.command_json)
    assert command["resource_decision"]["mode"] == "auto"
    assert command["resource_decision"]["effective_workers"] == 4
    assert command["resource_decision"]["effective_svt_lp"] == 2
    assert command["resource_decision"]["reason"] == "historical_estimate"
    assert command["resource_decision"]["evidence_count"] == 3
    assert command["av1an_argv"][command["av1an_argv"].index("--workers") + 1] == "4"
    assert "--lp 2" in command["av1an_argv"][command["av1an_argv"].index("--video-params") + 1]


def test_encode_worker_calibrates_auto_plan_before_starting_production_encode(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from avarch.application.benchmark_samples import BenchmarkSample
    from avarch.application.calibration_orchestrator import (
        CalibrationExecutionResult,
        MeasuredResourceSelection,
    )
    from avarch.application.performance_candidates import generate_safe_concurrency_candidates
    from avarch.domain.resource_policy import parse_resource_intent

    config, plan = _stored_encode_job(tmp_path)
    plan = plan.model_copy(
        update={
            "av1an": plan.av1an.model_copy(
                update={"workers": "auto", "encoder_args": ["--preset", "6", "--crf", "28"]}
            )
        }
    )
    plan.artifacts.plan_json.write_text(canonical_json(plan), encoding="utf-8")
    snapshot = ResourceSnapshot(
        effective_cpu_count=8,
        effective_cpu_quota=8.0,
        effective_memory_bytes=16 * 1024**3,
        cpu_values=(ResourceValue("cgroup_v2", 8.0, ResourceConfidence.HIGH),),
        memory_values=(ResourceValue("cgroup_v2", 16 * 1024**3, ResourceConfidence.HIGH),),
    )
    monkeypatch.setattr(
        "avarch.adapters.scheduler_workers.effective_resource_snapshot",
        lambda: snapshot,
    )
    def fake_source_frame_rate(
        _session: Session,
        *,
        media_file: MediaFile,
        stream_index: int,
    ) -> float:
        del media_file, stream_index
        return 24.0

    monkeypatch.setattr(
        "avarch.adapters.scheduler_workers._source_frame_rate",
        fake_source_frame_rate,
    )
    candidates = generate_safe_concurrency_candidates(
        intent=parse_resource_intent(workers="auto"),
        capacity=ResourceCapacity(
            cheap_workers=1,
            av1an_jobs=1,
            file_ops=1,
            cpu_budget=8,
            memory_budget_bytes=16 * 1024**3,
        ),
    )
    winner = next(candidate for candidate in candidates if candidate.workers != "auto")
    order: list[str] = []

    def fake_run_calibration(**_kwargs: object) -> CalibrationExecutionResult:
        order.append("calibration")
        return CalibrationExecutionResult(
            status="completed",
            reason="winner",
            budget_seconds=6.0,
            sample=BenchmarkSample(0.0, 10.0, 240, 10),
            candidates=candidates,
            measurements=(),
            tournament=None,
            selection=MeasuredResourceSelection(
                candidate=winner,
                confidence=0.8,
                reason="calibration_measurement",
                evidence_count=2,
            ),
        )

    def fake_execute_plan(
        selected_plan: Any,
        *,
        cancellation_token: object | None = None,
        progress_sink: ProgressSink | None = None,
    ) -> object:
        del cancellation_token, progress_sink
        order.append("encode")
        assert selected_plan.av1an.workers == winner.workers
        return "completed"

    monkeypatch.setattr("avarch.adapters.scheduler_workers.run_calibration", fake_run_calibration)
    monkeypatch.setattr("avarch.adapters.scheduler_workers.execute_plan", fake_execute_plan)

    asyncio.run(execute_encode_job(job_id=1, runner_id="runner", config=config))

    engine = create_db_engine(config.database.url)
    with Session(engine) as session:
        attempt = session.get(JobAttempt, 1)
    assert order == ["calibration", "encode"]
    assert attempt is not None and attempt.command_json is not None
    command = json.loads(attempt.command_json)
    assert command["calibration"]["status"] == "completed"
    assert command["resource_decision"]["reason"] == "calibration_measurement"


def test_encode_worker_does_not_start_when_calibration_is_incomplete(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from avarch.application.benchmark_samples import BenchmarkSample
    from avarch.application.calibration_orchestrator import CalibrationExecutionResult
    from avarch.application.performance_candidates import generate_safe_concurrency_candidates
    from avarch.domain.resource_policy import parse_resource_intent

    config, plan = _stored_encode_job(tmp_path)
    plan = plan.model_copy(
        update={
            "av1an": plan.av1an.model_copy(
                update={"workers": "auto", "encoder_args": ["--preset", "6", "--crf", "28"]}
            )
        }
    )
    plan.artifacts.plan_json.write_text(canonical_json(plan), encoding="utf-8")
    snapshot = ResourceSnapshot(
        effective_cpu_count=8,
        effective_cpu_quota=8.0,
        effective_memory_bytes=16 * 1024**3,
        cpu_values=(ResourceValue("cgroup_v2", 8.0, ResourceConfidence.HIGH),),
        memory_values=(ResourceValue("cgroup_v2", 16 * 1024**3, ResourceConfidence.HIGH),),
    )
    monkeypatch.setattr(
        "avarch.adapters.scheduler_workers.effective_resource_snapshot",
        lambda: snapshot,
    )

    def fake_source_frame_rate(
        _session: Session,
        *,
        media_file: MediaFile,
        stream_index: int,
    ) -> float:
        del media_file, stream_index
        return 24.0

    monkeypatch.setattr(
        "avarch.adapters.scheduler_workers._source_frame_rate",
        fake_source_frame_rate,
    )
    candidates = generate_safe_concurrency_candidates(
        intent=parse_resource_intent(workers="auto"),
        capacity=ResourceCapacity(
            cheap_workers=1,
            av1an_jobs=1,
            file_ops=1,
            cpu_budget=7,
            memory_budget_bytes=14 * 1024**3,
        ),
    )
    execute_called = False

    def fake_run_calibration(**_kwargs: object) -> CalibrationExecutionResult:
        return CalibrationExecutionResult(
            status="incomplete",
            reason="candidate_set_incomplete",
            budget_seconds=120.0,
            sample=BenchmarkSample(0.0, 10.0, 240, 10),
            candidates=candidates,
            measurements=(),
            tournament=None,
            selection=None,
        )

    def fake_execute_plan(
        _plan: Any,
        *,
        cancellation_token: object | None = None,
        progress_sink: ProgressSink | None = None,
    ) -> object:
        nonlocal execute_called
        del cancellation_token, progress_sink
        execute_called = True
        return "completed"

    monkeypatch.setattr("avarch.adapters.scheduler_workers.run_calibration", fake_run_calibration)
    monkeypatch.setattr("avarch.adapters.scheduler_workers.execute_plan", fake_execute_plan)

    asyncio.run(execute_encode_job(job_id=1, runner_id="runner", config=config))

    engine = create_db_engine(config.database.url)
    with Session(engine) as session:
        job = session.get(Job, 1)
        attempt = session.get(JobAttempt, 1)
        progress = session.get(JobAttemptProgress, 1)
    assert execute_called is False
    assert job is not None and job.status == JobStatus.FAILED
    assert attempt is not None and attempt.command_json is not None
    assert progress is not None and ProgressPhase(progress.phase) == ProgressPhase.FAILED
    assert "Production encode was not started" in (progress.message or "")


def test_encode_worker_persists_preparing_then_encoding_progress(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, plan = _stored_encode_job(tmp_path)
    observed_phases: list[ProgressPhase] = []

    def fake_execute_plan(
        _plan: object,
        *,
        cancellation_token: object | None = None,
        progress_sink: ProgressSink | None = None,
    ) -> object:
        del cancellation_token
        engine = create_db_engine(config.database.url)
        with Session(engine) as session:
            progress = session.get(JobAttemptProgress, 1)
            assert progress is not None
            observed_phases.append(ProgressPhase(progress.phase))
            assert progress.phase_started_at == progress.observed_at
            assert progress.heartbeat_at == progress.observed_at
        assert progress_sink is not None
        progress_sink.publish(_phase_snapshot(ProgressPhase.ENCODING))
        progress_sink.publish(_phase_snapshot(ProgressPhase.ENCODING))
        return "completed"

    monkeypatch.setattr("avarch.adapters.scheduler_workers.execute_plan", fake_execute_plan)

    asyncio.run(execute_encode_job(job_id=1, runner_id="runner", config=config))

    engine = create_db_engine(config.database.url)
    with Session(engine) as session:
        progress = session.get(JobAttemptProgress, 1)

    assert observed_phases == [ProgressPhase.PREPARING]
    assert progress is not None
    assert ProgressPhase(progress.phase) == ProgressPhase.COMPLETED
    assert progress.updated_at >= progress.created_at
    assert plan.plan_hash


def test_encode_worker_persists_numeric_av1an_progress(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, _plan = _stored_encode_job(tmp_path)

    def fake_execute_plan(
        _plan: object,
        *,
        cancellation_token: object | None = None,
        progress_sink: ProgressSink | None = None,
    ) -> object:
        del cancellation_token
        assert progress_sink is not None
        progress_sink.publish(_numeric_encoding_snapshot(current=12, total=120))
        progress_sink.publish(_numeric_encoding_snapshot(current=48, total=120))
        return "completed"

    monkeypatch.setattr("avarch.adapters.scheduler_workers.execute_plan", fake_execute_plan)

    asyncio.run(execute_encode_job(job_id=1, runner_id="runner", config=config))

    engine = create_db_engine(config.database.url)
    with Session(engine) as session:
        progress = session.get(JobAttemptProgress, 1)
        observation = session.exec(select(PerformanceObservation)).one()

    assert progress is not None
    assert ProgressPhase(progress.phase) == ProgressPhase.COMPLETED
    assert progress.current_value == 48.0
    assert progress.total_value == 120.0
    assert progress.advanced_at is not None
    assert observation.attempt_id == 1
    assert observation.total_frames == 48
    assert observation.incomplete is False
    assert observation.plan_hash is not None
    assert observation.resource_decision_json is not None
    assert observation.environment_signature_hash is not None
    assert observation.environment_signature_json is not None
    assert observation.workload_signature_hash is not None
    assert observation.workload_signature_json is not None


def test_encode_worker_ignores_external_progress_sink_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, _plan = _stored_encode_job(tmp_path)

    class BrokenSink:
        def publish(self, snapshot: ProgressSnapshot) -> None:
            del snapshot
            raise RuntimeError("observer failed")

    def fake_execute_plan(
        _plan: object,
        *,
        cancellation_token: object | None = None,
        progress_sink: ProgressSink | None = None,
    ) -> object:
        del cancellation_token
        assert progress_sink is not None
        progress_sink.publish(_phase_snapshot(ProgressPhase.ENCODING))
        return "completed"

    monkeypatch.setattr("avarch.adapters.scheduler_workers.execute_plan", fake_execute_plan)

    asyncio.run(
        execute_encode_job(
            job_id=1,
            runner_id="runner",
            config=config,
            progress_sink=BrokenSink(),
        )
    )

    engine = create_db_engine(config.database.url)
    with Session(engine) as session:
        progress = session.get(JobAttemptProgress, 1)

    assert progress is not None
    assert ProgressPhase(progress.phase) == ProgressPhase.COMPLETED


def test_validation_worker_persists_validating_then_completed_progress(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, plan = _stored_validation_job(tmp_path)
    observed_phases: list[ProgressPhase] = []

    async def fake_validate_output(**_kwargs: object) -> ValidationReport:
        engine = create_db_engine(config.database.url)
        with Session(engine) as session:
            progress = session.get(JobAttemptProgress, 1)
            assert progress is not None
            observed_phases.append(ProgressPhase(progress.phase))
        return _validation_report(plan=plan, now=datetime.now(UTC))

    monkeypatch.setattr("avarch.adapters.scheduler_workers.validate_output", fake_validate_output)

    result = asyncio.run(execute_validation_job(job_id=1, runner_id="runner", config=config))

    engine = create_db_engine(config.database.url)
    with Session(engine) as session:
        progress = session.get(JobAttemptProgress, 1)
        events = session.exec(
            select(JobEvent).where(JobEvent.job_id == 1).order_by(col(JobEvent.id))
        ).all()

    assert result is not None
    assert observed_phases == [ProgressPhase.VALIDATING]
    assert progress is not None
    assert ProgressPhase(progress.phase) == ProgressPhase.COMPLETED
    assert [JobEventType(event.event_type) for event in events] == [
        JobEventType.STAGE_STARTED,
        JobEventType.STAGE_COMPLETED,
    ]
    completion_details = json.loads(events[-1].details_json or "{}")
    assert completion_details["passed"] is True
    assert completion_details["size_decision"] == "accept"
    assert completion_details["source_safety_outcome"] == "original_retained"


def test_validation_worker_records_failure_lifecycle_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, _plan = _stored_validation_job(tmp_path)

    async def fake_validate_output(**_kwargs: object) -> ValidationReport:
        raise ValidationExecutionError("decode sample failed")

    monkeypatch.setattr("avarch.adapters.scheduler_workers.validate_output", fake_validate_output)

    result = asyncio.run(execute_validation_job(job_id=1, runner_id="runner", config=config))

    engine = create_db_engine(config.database.url)
    with Session(engine) as session:
        events = session.exec(
            select(JobEvent).where(JobEvent.job_id == 1).order_by(col(JobEvent.id))
        ).all()

    assert result is None
    assert [JobEventType(event.event_type) for event in events] == [
        JobEventType.STAGE_STARTED,
        JobEventType.STAGE_FAILED,
    ]
    failure_details = json.loads(events[-1].details_json or "{}")
    assert failure_details["error_type"] == "ValidationExecutionError"


def test_size_rejection_records_validation_and_cleanup_lifecycle_events(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, plan = _stored_validation_job(tmp_path)

    async def fake_validate_output(**_kwargs: object) -> ValidationReport:
        return _validation_report(
            plan=plan,
            now=datetime.now(UTC),
            output_size_bytes=plan.validation.source_size_bytes + 1,
        )

    monkeypatch.setattr("avarch.adapters.scheduler_workers.validate_output", fake_validate_output)

    validation = asyncio.run(execute_validation_job(job_id=1, runner_id="runner", config=config))
    asyncio.run(execute_cleanup_job(job_id=1, runner_id="runner", config=config))

    engine = create_db_engine(config.database.url)
    with Session(engine) as session:
        job = session.get(Job, 1)
        events = session.exec(
            select(JobEvent).where(JobEvent.job_id == 1).order_by(col(JobEvent.id))
        ).all()

    assert validation is not None
    assert job is not None
    assert job.status == JobStatus.SIZE_REJECTED
    cleanup_event = events[-1]
    validation_event = events[1]
    validation_details = json.loads(validation_event.details_json or "{}")
    cleanup_details = json.loads(cleanup_event.details_json or "{}")
    assert JobEventType(validation_event.event_type) == JobEventType.STAGE_COMPLETED
    assert validation_details["size_decision"] == "reject_not_smaller"
    assert JobStage(cleanup_event.stage) == JobStage.CLEANUP
    assert JobEventType(cleanup_event.event_type) == JobEventType.STAGE_COMPLETED
    assert cleanup_details["size_decision"] == "reject_not_smaller"
    assert cleanup_details["source_safety_outcome"] == "original_retained"


def test_rejected_output_cleanup_failure_records_lifecycle_event(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config, plan = _stored_validation_job(tmp_path)

    async def fake_validate_output(**_kwargs: object) -> ValidationReport:
        return _validation_report(
            plan=plan,
            now=datetime.now(UTC),
            output_size_bytes=plan.validation.source_size_bytes + 1,
        )

    monkeypatch.setattr("avarch.adapters.scheduler_workers.validate_output", fake_validate_output)

    validation = asyncio.run(execute_validation_job(job_id=1, runner_id="runner", config=config))
    plan.output_path.unlink()
    plan.output_path.mkdir()
    asyncio.run(execute_cleanup_job(job_id=1, runner_id="runner", config=config))

    engine = create_db_engine(config.database.url)
    with Session(engine) as session:
        events = session.exec(
            select(JobEvent).where(JobEvent.job_id == 1).order_by(col(JobEvent.id))
        ).all()

    assert validation is not None
    cleanup_event = events[-1]
    cleanup_details = json.loads(cleanup_event.details_json or "{}")
    assert JobStage(cleanup_event.stage) == JobStage.CLEANUP
    assert JobEventType(cleanup_event.event_type) == JobEventType.STAGE_FAILED
    assert cleanup_details["error_type"] == "RejectedOutputCleanupError"
    assert cleanup_details["source_safety_outcome"] == "original_retained"


def test_scheduler_pending_work_respects_disabled_promotion_stage(tmp_path: Path) -> None:
    database_path = tmp_path / "avarch.adapters.sqlite.db"
    config = AppConfig(database=DatabaseSettings(url=f"sqlite:///{database_path}"))
    engine = create_db_engine(config.database.url)
    create_db_schema(engine)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        media_file = _media_file(tmp_path / "movie-a.mkv", now)
        session.add(media_file)
        session.flush()
        session.add(
            _job(
                media_file.id or 0,
                now,
                queue_key="promote",
                status=JobStatus.QUEUED,
                stage=JobStage.PROMOTE,
            )
        )

    all_stages_store = SqliteSchedulerRunStore(config)
    no_promotion_store = SqliteSchedulerRunStore(
        config,
        claimable_stages=frozenset(
            {
                JobStage.PROBE,
                JobStage.PLAN,
                JobStage.ENCODE,
                JobStage.VALIDATE,
                JobStage.CLEANUP,
            }
        ),
    )

    assert all_stages_store.has_pending_jobs() is True
    assert no_promotion_store.has_pending_jobs() is False


class _PromotionWorkflowStub:
    def preflight(
        self,
        *,
        job_id: int,
        mode: PromotionMode,
        config: AppConfig,
        operation_id: str,
    ) -> PromotionPreflightView:
        del config, operation_id
        return PromotionPreflightView(
            job_id=job_id,
            validation_result_id=1,
            mode=mode,
            source_path=Path(),
            validated_output_path=Path(),
            final_path=Path(),
            staging_path=Path(),
            backup_path=None,
            warnings=(),
        )

    async def execute(
        self,
        *,
        job_id: int,
        mode: PromotionMode,
        config: AppConfig,
        owner_token: str,
    ) -> PromotionRecordView:
        del mode, config, owner_token
        return _promotion_record(job_id)

    async def recover(
        self,
        *,
        job_id: int,
        config: AppConfig,
        owner_token: str,
    ) -> PromotionRecordView:
        del config, owner_token
        return _promotion_record(job_id)

    async def promote(
        self,
        *,
        job_id: int,
        config: AppConfig,
        mode: PromotionMode = PromotionMode.REPLACE_ATOMIC,
        owner_token: str | None = None,
    ) -> PromotionResultView:
        del config, mode, owner_token
        return PromotionResultView(
            job_id=job_id,
            promotion_id=0,
            status=PromotionStatus.COMPLETED,
            final_path=Path(),
            promoted=True,
        )


def _engine(tmp_path: Path) -> Any:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'avarch.adapters.sqlite.db'}")
    create_db_schema(engine)
    return engine


def _stored_encode_job(tmp_path: Path) -> tuple[AppConfig, Any]:
    from tests.test_execution import _sample_plan  # pyright: ignore[reportPrivateUsage]

    database_path = tmp_path / "avarch.adapters.sqlite.db"
    config = AppConfig(database=DatabaseSettings(url=f"sqlite:///{database_path}"))
    engine = create_db_engine(config.database.url)
    create_db_schema(engine)
    plan = _sample_plan(tmp_path)
    plan.artifacts.plan_json.parent.mkdir(parents=True, exist_ok=True)
    plan.artifacts.plan_json.write_text(canonical_json(plan), encoding="utf-8")
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        media_file = MediaFile(
            path=str(plan.input_path),
            size_bytes=plan.input_path.stat().st_size,
            mtime_ns=plan.input_path.stat().st_mtime_ns,
            device_id=plan.input_path.stat().st_dev,
            inode=plan.input_path.stat().st_ino,
            fs_fingerprint=plan.source_fs_fingerprint,
            discovered_at=now,
            last_seen_at=now,
            status=MediaFileStatus.PRESENT,
        )
        session.add(media_file)
        session.flush()
        session.add(
            Job(
                media_file_id=media_file.id or 0,
                profile_name=plan.profile_name,
                profile_hash=plan.profile_hash,
                source_fs_fingerprint=plan.source_fs_fingerprint,
                queue_key="encode",
                plan_hash=plan.plan_hash,
                plan_path=str(plan.artifacts.plan_json),
                status=JobStatus.QUEUED,
                stage=JobStage.ENCODE,
                created_at=now,
                updated_at=now,
            )
        )
    return config, plan


def _stored_validation_job(tmp_path: Path) -> tuple[AppConfig, Any]:
    config, plan = _stored_encode_job(tmp_path)
    plan.output_path.parent.mkdir(parents=True, exist_ok=True)
    plan.output_path.write_bytes(b"validated output")
    engine = create_db_engine(config.database.url)
    with Session(engine) as session, session.begin():
        job = session.get(Job, 1)
        assert job is not None
        job.status = JobStatus.ENCODED
        job.stage = JobStage.VALIDATE
        job.output_path = str(plan.output_path)
        job.updated_at = datetime.now(UTC)
        session.add(job)
    return config, plan


def _add_resource_history(
    *,
    config: AppConfig,
    plan: Any,
    snapshot: ResourceSnapshot,
) -> None:
    engine = create_db_engine(config.database.url)
    now = datetime.now(UTC)
    environment = build_execution_environment_signature(
        snapshot=snapshot,
        tool_versions={
            "av1an_version_family": plan.execution_identity.av1an_version_family,
            "vapoursynth_version": plan.vapoursynth.vapoursynth_version,
        },
    )
    workload = build_workload_signature(plan)
    with Session(engine) as session, session.begin():
        for number, workers, svt_lp, fps in (
            (10, "auto", "native", 10.0),
            (11, "auto", "native", 10.0),
            (12, "auto", "native", 10.0),
            (13, 4, 2, 20.0),
            (14, 4, 2, 20.0),
            (15, 4, 2, 20.0),
        ):
            attempt = JobAttempt(
                job_id=1,
                attempt_number=number,
                stage=JobStage.ENCODE,
                resource_class=ResourceClass.HEAVY_AV1AN,
                status=AttemptStatus.COMPLETED,
                runner_id=f"history-{number}",
                started_at=now,
                finished_at=now,
            )
            session.add(attempt)
            session.flush()
            assert attempt.id is not None
            session.add(
                PerformanceObservation(
                    schema_version=1,
                    job_id=1,
                    attempt_id=attempt.id,
                    plan_hash=plan.plan_hash,
                    semantic_hash=plan.semantic_hash,
                    resource_policy_hash=plan.resource_policy_hash,
                    resource_decision_json=canonical_json(
                        {
                            "algorithm_version": RESOURCE_DECISION_ALGORITHM_VERSION,
                            "effective_workers": workers,
                            "effective_svt_lp": svt_lp,
                        }
                    ),
                    tool_versions_json=None,
                    environment_signature_hash=environment.signature_hash,
                    environment_signature_json=canonical_json(environment.to_payload()),
                    workload_signature_hash=workload.signature_hash,
                    workload_signature_json=canonical_json(workload.to_payload()),
                    total_frames=1200,
                    observation_duration_seconds=60.0,
                    aggregate_fps=fps,
                    peak_rss_bytes=1024,
                    peak_cgroup_memory_bytes=1024,
                    average_cpu_utilization_percent=80.0,
                    swap_current_bytes_delta=0,
                    cpu_throttled_events_delta=0,
                    cpu_throttled_usec_delta=0,
                    memory_oom_events_delta=0,
                    memory_oom_kill_events_delta=0,
                    resource_attribution_available=True,
                    incomplete=False,
                    progress_samples_observed=2,
                    resource_samples_observed=0,
                    warmup_seconds=0.0,
                    created_at=now,
                )
            )


def _validation_report(
    *,
    plan: Any,
    now: datetime,
    output_size_bytes: int | None = None,
) -> ValidationReport:
    return ValidationReport(
        plan_hash=plan.plan_hash,
        policy_hash=plan.validation.policy_hash,
        source_path=plan.input_path,
        output_path=plan.output_path,
        source_fs_fingerprint_before=plan.source_fs_fingerprint,
        source_fs_fingerprint_after=plan.source_fs_fingerprint,
        output_fs_fingerprint_before="output-before",
        output_fs_fingerprint_after="output-after",
        passed=True,
        checks=[
            ValidationCheck(
                name="output_exists",
                status=ValidationCheckStatus.PASS,
                required=True,
                expected=True,
                observed=True,
            )
        ],
        warnings=[],
        observed=ObservedValidationMedia(
            output_size_bytes=output_size_bytes
            if output_size_bytes is not None
            else plan.output_path.stat().st_size,
            container="matroska,webm",
            duration_seconds=plan.validation.source_duration_seconds,
            video_streams=[],
            audio_streams=[],
            subtitle_streams=[],
        ),
        started_at=now,
        finished_at=now,
    )


def _phase_snapshot(phase: ProgressPhase) -> ProgressSnapshot:
    now = datetime.now(UTC)
    return ProgressSnapshot(
        phase=phase,
        current=None,
        total=None,
        unit=None,
        rate_per_second=None,
        speed_ratio=None,
        source=ProgressSource.SCHEDULER,
        message=None,
        phase_started_at=now,
        observed_at=now,
        heartbeat_at=now,
        advanced_at=None,
    )


def _numeric_encoding_snapshot(*, current: int, total: int) -> ProgressSnapshot:
    now = datetime.now(UTC)
    return ProgressSnapshot(
        phase=ProgressPhase.ENCODING,
        current=current,
        total=total,
        unit=ProgressUnit.FRAMES,
        rate_per_second=60.0,
        speed_ratio=None,
        source=ProgressSource.AV1AN_OUTPUT,
        message="0/1 chunks",
        phase_started_at=now,
        observed_at=now,
        heartbeat_at=now,
        advanced_at=now,
    )


def _promotion_record(job_id: int) -> PromotionRecordView:
    return PromotionRecordView(
        id=1,
        job_id=job_id,
        mode=PromotionMode.REPLACE_ATOMIC,
        source_path="",
        backup_path=None,
        final_path="",
        validation_result_id=1,
        cleanup_completed=True,
        cleanup_error=None,
    )


def _media_file(path: Path, now: datetime) -> MediaFile:
    return MediaFile(
        path=str(path),
        size_bytes=1,
        mtime_ns=2,
        device_id=3,
        inode=4,
        fs_fingerprint=f"fingerprint-{path.name}",
        discovered_at=now,
        last_seen_at=now,
        status=MediaFileStatus.PRESENT,
    )


def _job(
    media_file_id: int,
    now: datetime,
    *,
    queue_key: str,
    status: JobStatus = JobStatus.QUEUED,
    stage: JobStage = JobStage.ENCODE,
) -> Job:
    return Job(
        media_file_id=media_file_id,
        profile_name="av1_1080p_sdr",
        profile_hash="profile-hash",
        source_fs_fingerprint="fingerprint",
        queue_key=queue_key,
        status=status,
        stage=stage,
        created_at=now,
        updated_at=now,
    )
