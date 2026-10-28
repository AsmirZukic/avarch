# Fault injection deliberately targets private transaction boundaries.
# pyright: reportPrivateUsage=false
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from avarch.adapters import promotion_service as service
from avarch.adapters.sqlite.db import create_db_engine
from avarch.adapters.sqlite.models import JobEvent, PromotionRecord
from avarch.config import AppConfig
from avarch.domain.jobs import JobEventType, JobStage
from avarch.models.promotion import PromotionMode, PromotionPhase, PromotionStatus
from tests.promotion_helpers import fake_replacement_decode
from tests.promotion_helpers import ready_job as _ready_job


class PowerLoss(BaseException):
    """Bypass exception cleanup to leave the actual persisted phase behind."""


@pytest.fixture(autouse=True)
def fake_media_decode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "validate_replacement_media", fake_replacement_decode)


def _expire(config: AppConfig) -> Engine:
    engine = create_db_engine(config.database.url)
    with Session(engine) as session, session.begin():
        record = session.exec(select(PromotionRecord)).one()
        record.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        session.add(record)
    return engine


@pytest.mark.parametrize(
    "phase",
    [
        PromotionPhase.STAGING,
        PromotionPhase.STAGED,
        PromotionPhase.BACKUP_PENDING,
        PromotionPhase.BACKUP_CREATED,
        PromotionPhase.INSTALL_PENDING,
        PromotionPhase.FINAL_INSTALLED,
        PromotionPhase.VERIFIED,
    ],
)
def test_restart_at_persisted_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: PromotionPhase,
) -> None:
    config, job_id, source, _output = _ready_job(tmp_path, output_bytes=b"encoded")
    write_journal = service._write_journal_from_record

    def interrupt(engine: Engine, promotion_id: int) -> None:
        write_journal(engine, promotion_id)
        with Session(engine) as session:
            record = session.get(PromotionRecord, promotion_id)
            assert record is not None
            if record.phase == phase:
                raise PowerLoss()

    monkeypatch.setattr(service, "_write_journal_from_record", interrupt)
    with pytest.raises(PowerLoss):
        asyncio.run(service.promote_job(job_id, config=config, mode=PromotionMode.REPLACE_ATOMIC))
    monkeypatch.setattr(service, "_write_journal_from_record", write_journal)
    engine = _expire(config)
    record = asyncio.run(
        service.recover_promotion(job_id=job_id, config=config, owner_token="restart")
    )
    assert record.status == PromotionStatus.COMPLETED
    assert source.read_bytes() == b"encoded"
    with Session(engine) as session:
        completions = session.exec(
            select(JobEvent).where(
                JobEvent.stage == JobStage.PROMOTE,
                JobEvent.event_type == JobEventType.STAGE_COMPLETED,
            )
        ).all()
        assert len(completions) == 1


@pytest.mark.parametrize("keep", [False, True])
def test_restart_after_install_before_recording_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    keep: bool,
) -> None:
    config, job_id, source, _output = _ready_job(tmp_path, output_bytes=b"encoded")
    mode = PromotionMode.KEEP_ORIGINAL if keep else PromotionMode.REPLACE_ATOMIC
    original_link = service.os.link
    original_replace = service.os.replace
    original = original_link if keep else original_replace

    def interrupt(src: Path, dst: Path, *, follow_symlinks: bool = False) -> None:
        if keep:
            original_link(src, dst, follow_symlinks=follow_symlinks)
        else:
            original_replace(src, dst)
        if (keep and str(dst).endswith(".av1.mkv")) or (not keep and Path(dst) == source):
            raise PowerLoss()

    name = "link" if keep else "replace"
    monkeypatch.setattr(service.os, name, interrupt)
    with pytest.raises(PowerLoss):
        asyncio.run(service.promote_job(job_id, config=config, mode=mode))
    monkeypatch.setattr(service.os, name, original)
    _expire(config)
    record = asyncio.run(
        service.recover_promotion(job_id=job_id, config=config, owner_token="restart")
    )
    assert record.status == PromotionStatus.COMPLETED
    assert Path(record.final_path).read_bytes() == b"encoded"
    if keep:
        assert source.read_bytes() == b"original"


def test_restart_after_commit_only_runs_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, job_id, source, encoded = _ready_job(tmp_path, output_bytes=b"encoded")
    cleanup = service._finish_cleanup

    def interrupt(*args: object) -> None:
        raise PowerLoss()

    monkeypatch.setattr(service, "_finish_cleanup", interrupt)
    with pytest.raises(PowerLoss):
        asyncio.run(service.promote_job(job_id, config=config, mode=PromotionMode.REPLACE_ATOMIC))
    monkeypatch.setattr(service, "_finish_cleanup", cleanup)
    # A committed recovery must not need to stage again, even if work output vanished.
    encoded.unlink()
    _expire(config)
    record = asyncio.run(
        service.recover_promotion(job_id=job_id, config=config, owner_token="restart")
    )
    assert record.cleanup_completed
    assert source.read_bytes() == b"encoded"


def test_restart_during_rollback_restores_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, job_id, source, _encoded = _ready_job(tmp_path, output_bytes=b"encoded")
    replace = service.os.replace
    write_journal = service._write_journal_from_record

    def corrupt(src: Path, dst: Path) -> None:
        replace(src, dst)
        if Path(dst) == source:
            source.write_bytes(b"damaged")

    def interrupt(engine: Engine, promotion_id: int) -> None:
        write_journal(engine, promotion_id)
        with Session(engine) as session:
            record = session.get(PromotionRecord, promotion_id)
            assert record is not None
            if record.phase == PromotionPhase.ROLLBACK_PENDING:
                raise PowerLoss()

    monkeypatch.setattr(service.os, "replace", corrupt)
    monkeypatch.setattr(service, "_write_journal_from_record", interrupt)
    with pytest.raises(PowerLoss):
        asyncio.run(service.promote_job(job_id, config=config, mode=PromotionMode.REPLACE_ATOMIC))
    monkeypatch.setattr(service.os, "replace", replace)
    monkeypatch.setattr(service, "_write_journal_from_record", write_journal)
    _expire(config)
    record = asyncio.run(
        service.recover_promotion(job_id=job_id, config=config, owner_token="restart")
    )
    assert record.status == PromotionStatus.ROLLED_BACK
    assert source.read_bytes() == b"original"
