"""Real decode through the destructive promotion path, not a pre-seeded outcome test."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session, select

from avarch.adapters.filesystem.plans import load_plan_artifact
from avarch.adapters.filesystem.scanner import create_file_snapshot
from avarch.adapters.promotion_service import promote_job
from avarch.adapters.sqlite.db import create_db_engine
from avarch.adapters.sqlite.models import Job, PromotionRecord, ValidationResult
from avarch.adapters.sqlite.promotion_approvals import approve_job_promotion
from avarch.config import AppConfig
from avarch.models.promotion import PromotionMode
from avarch.serialization import canonical_json
from tests.promotion_helpers import ready_job
from tests.test_replacement_validation import real_media as real_media


def media_job(tmp_path: Path, real_media: Path, *, truncated: bool) -> tuple[AppConfig, int, Path]:
    data = real_media.read_bytes()
    config, job_id, source, output = ready_job(
        tmp_path,
        output_bytes=data[: len(data) // 2] if truncated else data,
    )
    source.write_bytes(data)
    engine = create_db_engine(config.database.url)
    with Session(engine) as session, session.begin():
        job = session.get(Job, job_id)
        assert job is not None and job.plan_path is not None
        plan_path = Path(job.plan_path)
        plan = load_plan_artifact(plan_path)
        fingerprint = create_file_snapshot(source).fs_fingerprint
        plan = plan.model_copy(
            update={
                "source_fs_fingerprint": fingerprint,
                "audio": None,
                "video": plan.video.model_copy(update={"source_stream_index": 0}),
                "validation": plan.validation.model_copy(update={"source_duration_seconds": 6}),
            }
        )
        plan_path.write_text(canonical_json(plan))
        job.source_fs_fingerprint = fingerprint
        approve_job_promotion(
            session, job=job, mode=PromotionMode.REPLACE_ATOMIC, actor="user", now=datetime.now(UTC)
        )
        result = session.get(ValidationResult, job.latest_validation_id)
        assert result is not None
        # Model the metadata validator accepting the truncated container.
        assert result.passed
        result.output_fs_fingerprint = create_file_snapshot(output).fs_fingerprint
        session.add(result)
    return config, job_id, source


def test_truncated_metadata_validated_output_cannot_replace_original(
    tmp_path: Path,
    real_media: Path,
) -> None:
    config, job_id, source = media_job(tmp_path, real_media, truncated=True)
    before = source.read_bytes()
    result = asyncio.run(promote_job(job_id, config=config, mode=PromotionMode.REPLACE_ATOMIC))
    assert not result.promoted
    assert source.read_bytes() == before
    assert not source.with_name(source.name + ".avarch-original").exists()
    with Session(create_db_engine(config.database.url)) as session:
        record = session.exec(select(PromotionRecord)).one()
        assert record.replacement_evidence_json is None


def test_real_complete_output_replaces_but_keeps_unapproved_backup(
    tmp_path: Path,
    real_media: Path,
) -> None:
    config, job_id, source = media_job(tmp_path, real_media, truncated=False)
    before = source.read_bytes()
    result = asyncio.run(promote_job(job_id, config=config, mode=PromotionMode.REPLACE_ATOMIC))
    assert result.promoted
    assert source.with_name(source.name + ".avarch-original").read_bytes() == before
    with Session(create_db_engine(config.database.url)) as session:
        record = session.exec(select(PromotionRecord)).one()
        assert record.replacement_evidence_json is not None
        assert '"decoded_video_frames":60' in record.replacement_evidence_json
        assert not record.delete_backup_approved
