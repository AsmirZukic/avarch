"""Execute a mixed post-encode batch against real FFmpeg media and one queue.

Av1an itself is deliberately outside this test; validation/promotion/cleanup are real.
"""

from __future__ import annotations

import asyncio
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session, select

from avarch.adapters.filesystem.plans import load_plan_artifact
from avarch.adapters.filesystem.scanner import create_file_snapshot
from avarch.adapters.promotion import PromotionWorkflowAdapter
from avarch.adapters.scheduler_workers import (
    execute_cleanup_job,
    execute_promotion_job,
    execute_validation_job,
)
from avarch.adapters.sqlite.db import create_db_engine, create_db_schema
from avarch.adapters.sqlite.models import Job, MediaFile, MediaFileStatus, ValidationResult
from avarch.adapters.sqlite.queue import claimable_jobs
from avarch.config import AppConfig, DatabaseSettings
from avarch.domain.jobs import JobStage, JobStatus
from avarch.serialization import canonical_json
from tests.promotion_helpers import ready_job
from tests.test_replacement_validation import real_media as real_media


def test_post_encode_batch_runs_success_failure_and_size_rejection(
    tmp_path: Path,
    real_media: Path,
) -> None:
    config = AppConfig(database=DatabaseSettings(url=f"sqlite:///{tmp_path / 'batch.db'}"))
    engine = create_db_engine(config.database.url)
    create_db_schema(engine)
    originals: dict[str, bytes] = {}
    outputs: dict[str, Path] = {}
    now = datetime.now(UTC)
    for case in ("success", "corrupt", "too-large"):
        folder = tmp_path / case
        folder.mkdir()
        scaffold_config, scaffold_id, source, output = ready_job(
            folder, output_bytes=real_media.read_bytes()
        )
        if case == "too-large":
            source.write_bytes(real_media.read_bytes())
        else:
            source.unlink()
            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-i",
                    str(real_media),
                    "-c:v",
                    "ffv1",
                    str(source),
                ],
                check=True,
                timeout=60,
            )
        if case == "corrupt":
            output.write_bytes(b"not media")
        originals[str(source)] = source.read_bytes()
        outputs[case] = output
        with Session(create_db_engine(scaffold_config.database.url)) as scaffold:
            scaffold_job = scaffold.get(Job, scaffold_id)
            assert scaffold_job is not None and scaffold_job.plan_path is not None
            plan_path = Path(scaffold_job.plan_path)
            plan = load_plan_artifact(plan_path)
        snapshot = create_file_snapshot(source)
        with Session(engine) as session, session.begin():
            media = MediaFile(
                path=str(source),
                size_bytes=snapshot.size_bytes,
                mtime_ns=snapshot.mtime_ns,
                device_id=snapshot.device_id,
                inode=snapshot.inode,
                fs_fingerprint=snapshot.fs_fingerprint,
                discovered_at=now,
                last_seen_at=now,
                status=MediaFileStatus.PRESENT,
            )
            session.add(media)
            session.flush()
            assert media.id is not None
            policy = plan.validation.model_copy(
                update={
                    "source_duration_seconds": 6,
                    "source_size_bytes": snapshot.size_bytes,
                    "expected_width": 64,
                    "expected_height": 64,
                    "expected_audio_stream_count": 0,
                    "expected_audio_codec": None,
                    "expected_audio_channels": None,
                    "expected_audio_language": None,
                    "expected_subtitles": [],
                    "minimum_output_bytes": 1,
                    "minimum_output_source_ratio": 0,
                    "minimum_size_reduction_percent": None,
                }
            )
            plan = plan.model_copy(
                update={
                    "plan_hash": f"batch-{case}",
                    "media_file_id": media.id,
                    "source_fs_fingerprint": snapshot.fs_fingerprint,
                    "validation": policy,
                }
            )
            plan_path.write_text(canonical_json(plan))
            job = Job(
                media_file_id=media.id,
                profile_name=plan.profile_name,
                profile_hash=plan.profile_hash,
                source_fs_fingerprint=snapshot.fs_fingerprint,
                queue_key=case,
                plan_hash=plan.plan_hash,
                plan_path=str(plan_path),
                output_path=str(output),
                status=JobStatus.ENCODED,
                stage=JobStage.VALIDATE,
                created_at=now,
                updated_at=now,
            )
            session.add(job)
            session.flush()
            assert job.id is not None

    async def run_batch() -> None:
        for _ in range(20):
            with Session(engine) as session:
                jobs = claimable_jobs(session, active_job_ids=set())
                work = [(job.id, job.stage) for job in jobs]
            if not work:
                return
            for job_id, stage in work:
                assert job_id is not None
                if stage == JobStage.VALIDATE:
                    await execute_validation_job(job_id=job_id, runner_id="batch", config=config)
                elif stage == JobStage.PROMOTE:
                    await execute_promotion_job(
                        job_id=job_id,
                        runner_id="batch",
                        config=config,
                        promotion_workflow=PromotionWorkflowAdapter(),
                    )
                elif stage == JobStage.CLEANUP:
                    await execute_cleanup_job(job_id=job_id, runner_id="batch", config=config)
                else:
                    raise AssertionError(f"Unexpected stage: {stage}")
        raise AssertionError("Batch did not drain")

    asyncio.run(run_batch())
    with Session(engine) as session:
        states = {job.queue_key: job.status for job in session.exec(select(Job)).all()}
        assert states == {
            "success": JobStatus.PROMOTED,
            "corrupt": JobStatus.FAILED,
            "too-large": JobStatus.SIZE_REJECTED,
        }
        assert len(session.exec(select(ValidationResult)).all()) == 3
    assert not outputs["too-large"].exists()
    assert outputs["corrupt"].exists()
    for path, contents in originals.items():
        assert Path(path).read_bytes() == contents
