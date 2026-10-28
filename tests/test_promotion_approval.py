from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlmodel import Session, select

from avarch.adapters.promotion import PromotionWorkflowAdapter
from avarch.adapters.promotion_service import promote_job
from avarch.adapters.scheduler_workers import execute_promotion_job
from avarch.adapters.sqlite.db import create_db_engine
from avarch.adapters.sqlite.models import Job, PromotionRecord
from avarch.adapters.sqlite.promotion_approvals import (
    PromotionApprovalError,
    approve_job_promotion,
    current_promotion_approval,
)
from avarch.models.promotion import PromotionMode
from tests.promotion_helpers import fake_replacement_decode
from tests.promotion_helpers import ready_job as _ready_job


@pytest.mark.parametrize(
    "mode", [PromotionMode.REPLACE_ATOMIC, PromotionMode.MOVE_ORIGINAL_TO_BACKUP]
)
def test_replacement_requires_job_specific_approval(tmp_path: Path, mode: PromotionMode) -> None:
    config, job_id, source, encoded = _ready_job(tmp_path, output_bytes=b"encoded")
    engine = create_db_engine(config.database.url)
    with Session(engine) as session, session.begin():
        job = session.get(Job, job_id)
        assert job is not None
        job.promotion_approval_json = None
        session.add(job)
    result = asyncio.run(promote_job(job_id, config=config, mode=mode))
    assert not result.promoted
    assert "approval stored" in (result.error_message or "")
    assert source.read_bytes() == b"original"
    assert encoded.exists()
    with Session(engine) as session:
        assert session.exec(select(PromotionRecord)).all() == []


@pytest.mark.parametrize("field", ["plan_hash", "source_fs_fingerprint"])
def test_changed_identity_invalidates_approval(tmp_path: Path, field: str) -> None:
    config, job_id, _source, _encoded = _ready_job(tmp_path, output_bytes=b"encoded")
    with Session(create_db_engine(config.database.url)) as session:
        job = session.get(Job, job_id)
        assert job is not None
        setattr(job, field, "changed")
        with pytest.raises(PromotionApprovalError, match="stale"):
            current_promotion_approval(job)


def test_starting_scheduler_does_not_approve_replacement(tmp_path: Path) -> None:
    config, job_id, source, _encoded = _ready_job(tmp_path, output_bytes=b"encoded")
    with Session(create_db_engine(config.database.url)) as session, session.begin():
        job = session.get(Job, job_id)
        assert job is not None
        job.promotion_approval_json = None
        session.add(job)
    asyncio.run(
        execute_promotion_job(
            job_id=job_id,
            runner_id="scheduler",
            config=config,
            promotion_workflow=PromotionWorkflowAdapter(),
        )
    )
    assert source.read_bytes() == b"original"
    with Session(create_db_engine(config.database.url)) as session:
        record = session.exec(select(PromotionRecord)).one()
        assert record.mode == PromotionMode.KEEP_ORIGINAL
        job = session.get(Job, job_id)
        assert job is not None and job.promotion_approval_json is None


def test_scheduler_uses_approved_mode_and_retains_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Byte fixtures exercise approval and transaction behavior; real decoding has separate tests.
    monkeypatch.setattr(
        "avarch.adapters.promotion_service.validate_replacement_media",
        fake_replacement_decode,
    )
    config, job_id, source, _encoded = _ready_job(tmp_path, output_bytes=b"encoded")
    with Session(create_db_engine(config.database.url)) as session, session.begin():
        job = session.get(Job, job_id)
        assert job is not None
        approve_job_promotion(
            session,
            job=job,
            mode=PromotionMode.MOVE_ORIGINAL_TO_BACKUP,
            actor="user",
            now=datetime.now(UTC),
        )
    asyncio.run(
        execute_promotion_job(
            job_id=job_id,
            runner_id="scheduler",
            config=config,
            promotion_workflow=PromotionWorkflowAdapter(),
        )
    )
    assert source.read_bytes() == b"encoded"
    assert source.with_name(source.name + ".avarch-original").read_bytes() == b"original"
    with Session(create_db_engine(config.database.url)) as session:
        record = session.exec(select(PromotionRecord)).one()
        assert record.mode == PromotionMode.MOVE_ORIGINAL_TO_BACKUP
        assert not record.delete_backup_approved
