from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session

from avarch.adapters.scheduler_run import SqliteSchedulerRunStore
from avarch.adapters.scheduler_workers import (
    execute_encode_job,
    execute_promotion_job,
    execute_validation_job,
)
from avarch.adapters.sqlite.db import create_db_engine, create_db_schema
from avarch.adapters.sqlite.models import Job, JobAttemptProgress, MediaFile, MediaFileStatus
from avarch.adapters.sqlite.queue import claimable_jobs
from avarch.application.progress import ProgressSink
from avarch.application.promotion import (
    PromotionPreflightView,
    PromotionRecordView,
    PromotionResultView,
)
from avarch.config import AppConfig, DatabaseSettings
from avarch.domain.jobs import JobStage, JobStatus
from avarch.domain.progress import ProgressPhase, ProgressSnapshot, ProgressSource
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
    monkeypatch: Any,
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
    monkeypatch: Any,
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
        await asyncio.wait_for(asyncio.to_thread(started.wait), timeout=1.0)
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
    monkeypatch: Any,
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


def test_encode_worker_persists_preparing_then_encoding_progress(
    monkeypatch: Any,
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


def test_encode_worker_ignores_external_progress_sink_failure(
    monkeypatch: Any,
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
    monkeypatch: Any,
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

    assert result is not None
    assert observed_phases == [ProgressPhase.VALIDATING]
    assert progress is not None
    assert ProgressPhase(progress.phase) == ProgressPhase.COMPLETED


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


def _validation_report(*, plan: Any, now: datetime) -> ValidationReport:
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
            output_size_bytes=plan.output_path.stat().st_size,
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
