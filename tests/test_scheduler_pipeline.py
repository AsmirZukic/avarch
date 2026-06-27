from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session

from avarch.adapters.scheduler_workers import execute_promotion_job
from avarch.adapters.sqlite.db import create_db_engine, create_db_schema
from avarch.adapters.sqlite.models import Job, MediaFile, MediaFileStatus
from avarch.adapters.sqlite.queue import claimable_jobs
from avarch.application.promotion import (
    PromotionPreflightView,
    PromotionRecordView,
    PromotionResultView,
)
from avarch.config import AppConfig, DatabaseSettings
from avarch.domain.jobs import JobStage, JobStatus
from avarch.domain.scheduler import ResourceCapacity, has_resource_capacity
from avarch.models.promotion import PromotionMode, PromotionStatus


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
