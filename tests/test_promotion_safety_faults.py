# Fault injection deliberately targets filesystem and persistence boundaries.
# pyright: reportPrivateUsage=false
from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import BinaryIO

import pytest
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from avarch.adapters import promotion_service as service
from avarch.adapters.promotion_types import PromotionLeaseError, PromotionRollbackError
from avarch.adapters.sqlite.db import create_db_engine
from avarch.adapters.sqlite.models import PromotionRecord
from avarch.models.promotion import FileStatSnapshot, PromotionMode, PromotionStatus
from tests.promotion_helpers import fake_replacement_decode, ready_job
from tests.test_promotion_crash_recovery import PowerLoss


@pytest.fixture(autouse=True)
def fake_decoder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "validate_replacement_media", fake_replacement_decode)


def test_staging_creation_race_does_not_delete_competing_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, job_id, source, _ = ready_job(tmp_path, output_bytes=b"encoded")
    create_staging = service.create_promotion_staging_file
    collided: list[Path] = []

    def race(path: Path, *, mode: int) -> BinaryIO:
        path.write_bytes(b"unrelated staging file")
        collided.append(path)
        return create_staging(path, mode=mode)

    monkeypatch.setattr(service, "create_promotion_staging_file", race)
    result = asyncio.run(service.promote_job(job_id, config=config))
    assert not result.promoted
    assert collided[0].read_bytes() == b"unrelated staging file"
    assert source.read_bytes() == b"original"


def test_rollback_does_not_overwrite_a_different_installed_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, job_id, source, _ = ready_job(tmp_path, output_bytes=b"encoded")
    replace = service.os.replace
    unrelated = tmp_path / "unrelated"
    unrelated.write_bytes(b"another owner")

    def replace_then_race(src: Path, dst: Path) -> None:
        replace(src, dst)
        if Path(dst) == source:
            replace(unrelated, source)

    monkeypatch.setattr(service.os, "replace", replace_then_race)
    result = asyncio.run(
        service.promote_job(job_id, config=config, mode=PromotionMode.REPLACE_ATOMIC)
    )
    assert not result.promoted
    assert source.read_bytes() == b"another owner"
    assert source.with_name(source.name + ".avarch-original").read_bytes() == b"original"
    with Session(create_db_engine(config.database.url)) as session:
        assert (
            session.exec(select(PromotionRecord)).one().status == PromotionStatus.RECOVERY_REQUIRED
        )


def test_backup_digest_detects_corruption_even_with_preserved_stat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, job_id, source, _ = ready_job(tmp_path, output_bytes=b"encoded")
    create_backup = service.create_original_rollback_link

    def corrupt_backup(*, source_path: Path, backup_path: Path, expected_source: FileStatSnapshot):
        create_backup(
            source_path=source_path, backup_path=backup_path, expected_source=expected_source
        )
        before = backup_path.stat()
        backup_path.write_bytes(b"corrupt!")  # Same length, and the hard link also changes source.
        os.utime(backup_path, ns=(before.st_atime_ns, before.st_mtime_ns))

    monkeypatch.setattr(service, "create_original_rollback_link", corrupt_backup)
    result = asyncio.run(
        service.promote_job(job_id, config=config, mode=PromotionMode.REPLACE_ATOMIC)
    )
    assert not result.promoted
    assert source.read_bytes() == b"corrupt!"  # Must not replace evidence of external corruption.
    assert "backup" in (result.error_message or "")


def test_recovery_refuses_unexpired_sqlite_lease(tmp_path: Path) -> None:
    config, job_id, source, _ = ready_job(tmp_path, output_bytes=b"encoded")
    engine = create_db_engine(config.database.url)
    with Session(engine) as session, session.begin():
        service.claim_promotion(
            session,
            job_id=job_id,
            mode=PromotionMode.REPLACE_ATOMIC,
            owner_token="live-owner",
            now=datetime.now(UTC),
        )
    with pytest.raises(PromotionLeaseError, match="still active"):
        asyncio.run(service.recover_promotion(job_id=job_id, config=config, owner_token="other"))
    assert source.read_bytes() == b"original"


def test_restart_after_restoration_before_rollback_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, job_id, source, _ = ready_job(tmp_path, output_bytes=b"encoded")
    replace = service.os.replace

    def interrupt(src: Path, dst: Path) -> None:
        replace(src, dst)
        if Path(dst) == source:
            if str(src).endswith(".avarch-original"):
                raise PowerLoss()
            source.write_bytes(b"damaged")

    monkeypatch.setattr(service.os, "replace", interrupt)
    with pytest.raises(PowerLoss):
        asyncio.run(service.promote_job(job_id, config=config, mode=PromotionMode.REPLACE_ATOMIC))
    monkeypatch.setattr(service.os, "replace", replace)
    engine = create_db_engine(config.database.url)
    with Session(engine) as session, session.begin():
        record = session.exec(select(PromotionRecord)).one()
        record.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        session.add(record)
    record = asyncio.run(
        service.recover_promotion(job_id=job_id, config=config, owner_token="restart")
    )
    assert record.status == PromotionStatus.ROLLED_BACK
    assert source.read_bytes() == b"original"


def test_unknown_partial_staging_is_retained_for_manual_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config, job_id, source, _ = ready_job(tmp_path, output_bytes=b"encoded")
    journal = service._write_journal_from_record

    def interrupt(engine: Engine, promotion_id: int) -> None:
        journal(engine, promotion_id)
        with Session(engine) as session:
            record = session.get(PromotionRecord, promotion_id)
            assert record is not None
            Path(record.staging_path).write_bytes(b"partial or unrelated")
        raise PowerLoss()

    monkeypatch.setattr(service, "_write_journal_from_record", interrupt)
    with pytest.raises(PowerLoss):
        asyncio.run(service.promote_job(job_id, config=config))
    monkeypatch.setattr(service, "_write_journal_from_record", journal)
    engine = create_db_engine(config.database.url)
    with Session(engine) as session, session.begin():
        record = session.exec(select(PromotionRecord)).one()
        record.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        staging = Path(record.staging_path)
        session.add(record)
    with pytest.raises(PromotionRollbackError, match="partial staging"):
        asyncio.run(service.recover_promotion(job_id=job_id, config=config, owner_token="restart"))
    assert staging.read_bytes() == b"partial or unrelated"
    assert source.read_bytes() == b"original"
