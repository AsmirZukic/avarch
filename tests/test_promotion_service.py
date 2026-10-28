from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlmodel import Session, col, select

from avarch.adapters.promotion_service import (
    claim_promotion,
    mark_promotion_failed_or_validated,
    promote_job,
    recover_promotion,
)
from avarch.adapters.promotion_types import StagedOutput
from avarch.adapters.sqlite.db import create_db_engine
from avarch.adapters.sqlite.job_transitions import JobTransitionError, transition_job
from avarch.adapters.sqlite.models import (
    Job,
    JobAttempt,
    JobAttemptProgress,
    JobEvent,
    PromotionRecord,
    ValidationResult,
)
from avarch.domain.jobs import (
    AttemptStatus,
    JobEventType,
    JobOutcomeReason,
    JobStage,
    JobStatus,
    ResourceClass,
)
from avarch.domain.progress import ProgressPhase
from avarch.models.promotion import PromotionMode, PromotionPhase, PromotionStatus
from tests.promotion_helpers import fake_replacement_decode
from tests.promotion_helpers import ready_job as _ready_job


@pytest.fixture(autouse=True)
def fake_media_decoder(monkeypatch: pytest.MonkeyPatch) -> None:
    # These are filesystem transaction tests with byte fixtures, not media evidence.
    monkeypatch.setattr(
        "avarch.adapters.promotion_service.validate_replacement_media",
        fake_replacement_decode,
    )


def test_promotion_replaces_original_with_encoded_file(tmp_path: Path) -> None:
    config, job_id, source, encoded = _ready_job(tmp_path, output_bytes=b"encoded")

    result = asyncio.run(promote_job(job_id, config=config, mode=PromotionMode.REPLACE_ATOMIC))

    assert result.promoted is True
    assert result.status == PromotionStatus.COMPLETED
    assert source.read_bytes() == b"encoded"
    assert not encoded.exists()


def test_default_promotion_preserves_original(tmp_path: Path) -> None:
    config, job_id, source, _encoded = _ready_job(tmp_path, output_bytes=b"encoded")
    result = asyncio.run(promote_job(job_id, config=config))
    assert result.promoted
    assert source.read_bytes() == b"original"
    assert result.final_path.read_bytes() == b"encoded"


def test_keep_original_refuses_destination_created_during_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from avarch.adapters import promotion_service as service
    from avarch.domain.promotion import derive_keep_original_path

    config, job_id, source, _encoded = _ready_job(tmp_path, output_bytes=b"encoded")
    destination = derive_keep_original_path(source, container="mkv")
    original_stage = service.stage_validated_output

    async def stage(**kwargs: Any) -> StagedOutput:
        result = await original_stage(**kwargs)
        destination.write_bytes(b"unrelated")
        return result

    monkeypatch.setattr(service, "stage_validated_output", stage)
    result = asyncio.run(promote_job(job_id, config=config))
    assert not result.promoted
    assert destination.read_bytes() == b"unrelated"
    assert source.read_bytes() == b"original"


def test_corruption_after_install_restores_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from avarch.adapters import promotion_service as service

    config, job_id, source, _encoded = _ready_job(tmp_path, output_bytes=b"encoded")
    original_replace = service.os.replace

    def replace(src: Path, dst: Path) -> None:
        original_replace(src, dst)
        if Path(dst) == source and ".avarch-original" not in str(src):
            source.write_bytes(b"damaged")

    monkeypatch.setattr(service.os, "replace", replace)
    result = asyncio.run(promote_job(job_id, config=config, mode=PromotionMode.REPLACE_ATOMIC))
    assert not result.promoted
    assert source.read_bytes() == b"original"
    with Session(create_db_engine(config.database.url)) as session:
        record = session.exec(select(PromotionRecord)).one()
        assert record.status == PromotionStatus.ROLLED_BACK


def test_failed_restoration_requires_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from avarch.adapters import promotion_service as service

    config, job_id, source, _encoded = _ready_job(tmp_path, output_bytes=b"encoded")
    original_replace = service.os.replace

    def replace(src: Path, dst: Path) -> None:
        if ".avarch-original" in str(src):
            raise OSError("injected restoration failure")
        original_replace(src, dst)
        if Path(dst) == source:
            source.write_bytes(b"damaged")

    monkeypatch.setattr(service.os, "replace", replace)
    result = asyncio.run(promote_job(job_id, config=config, mode=PromotionMode.REPLACE_ATOMIC))
    assert not result.promoted
    assert source.with_name(source.name + ".avarch-original").read_bytes() == b"original"
    with Session(create_db_engine(config.database.url)) as session:
        record = session.exec(select(PromotionRecord)).one()
        assert record.status == PromotionStatus.RECOVERY_REQUIRED
        assert record.phase == PromotionPhase.RECOVERY_REQUIRED


def test_promotion_claim_records_promoting_progress(tmp_path: Path) -> None:
    config, job_id, _source, _encoded = _ready_job(tmp_path, output_bytes=b"encoded")
    engine = create_db_engine(config.database.url)
    now = datetime.now(UTC)

    with Session(engine) as session, session.begin():
        record = claim_promotion(
            session,
            job_id=job_id,
            mode=PromotionMode.REPLACE_ATOMIC,
            owner_token="owner",
            now=now,
        )
        progress = session.get(JobAttemptProgress, record.attempt_id)
        progress_phase = ProgressPhase(progress.phase) if progress is not None else None

    assert progress is not None
    assert progress_phase == ProgressPhase.PROMOTING


def test_promotion_claim_records_start_lifecycle_event(tmp_path: Path) -> None:
    config, job_id, _source, _encoded = _ready_job(tmp_path, output_bytes=b"encoded")
    engine = create_db_engine(config.database.url)
    now = datetime.now(UTC)

    with Session(engine) as session, session.begin():
        record = claim_promotion(
            session,
            job_id=job_id,
            mode=PromotionMode.REPLACE_ATOMIC,
            owner_token="owner",
            now=now,
        )
        event = session.exec(select(JobEvent)).one()
        event_type = JobEventType(event.event_type)
        event_attempt_id = event.attempt_id
        event_stage = JobStage(event.stage)
        event_details = json.loads(event.details_json or "{}")
        record_attempt_id = record.attempt_id
        record_id = record.id

    assert event_type == JobEventType.STAGE_STARTED
    assert event_attempt_id == record_attempt_id
    assert event_stage == JobStage.PROMOTE
    assert event_details["promotion_id"] == record_id


def test_completed_promotion_records_completed_progress(tmp_path: Path) -> None:
    config, job_id, _source, _encoded = _ready_job(tmp_path, output_bytes=b"encoded")

    result = asyncio.run(promote_job(job_id, config=config, mode=PromotionMode.REPLACE_ATOMIC))

    engine = create_db_engine(config.database.url)
    with Session(engine) as session:
        record = session.get(PromotionRecord, result.promotion_id)
        assert record is not None
        progress = session.get(JobAttemptProgress, record.attempt_id)

    assert progress is not None
    assert ProgressPhase(progress.phase) == ProgressPhase.COMPLETED


def test_completed_promotion_records_actual_savings_and_source_safety(tmp_path: Path) -> None:
    config, job_id, _source, _encoded = _ready_job(tmp_path, output_bytes=b"enc")

    result = asyncio.run(promote_job(job_id, config=config, mode=PromotionMode.REPLACE_ATOMIC))

    engine = create_db_engine(config.database.url)
    with Session(engine) as session:
        events = session.exec(
            select(JobEvent).where(JobEvent.job_id == job_id).order_by(col(JobEvent.id))
        ).all()

    promotion_completion = [
        event
        for event in events
        if JobStage(event.stage) == JobStage.PROMOTE
        and JobEventType(event.event_type) == JobEventType.STAGE_COMPLETED
    ][0]
    details = json.loads(promotion_completion.details_json or "{}")
    assert result.promoted is True
    assert details["source_size_bytes"] == len(b"original")
    assert details["output_size_bytes"] == len(b"enc")
    assert details["saved_bytes"] == len(b"original") - len(b"enc")
    assert details["source_safety_outcome"] == "source_replaced_after_verified_backup"


def test_recovered_promotion_records_savings_once(tmp_path: Path) -> None:
    config, job_id, _source, _encoded = _ready_job(tmp_path, output_bytes=b"enc")
    engine = create_db_engine(config.database.url)
    now = datetime.now(UTC)

    with Session(engine) as session, session.begin():
        record = claim_promotion(
            session,
            job_id=job_id,
            mode=PromotionMode.REPLACE_ATOMIC,
            owner_token="first-owner",
            now=now,
        )
        record.lease_expires_at = now - timedelta(seconds=60)
        session.add(record)

    recovered = asyncio.run(
        recover_promotion(job_id=job_id, config=config, owner_token="second-owner")
    )

    with Session(engine) as session:
        completion_events = session.exec(
            select(JobEvent).where(
                JobEvent.job_id == job_id,
                JobEvent.stage == JobStage.PROMOTE,
                JobEvent.event_type == JobEventType.STAGE_COMPLETED,
            )
        ).all()

    assert recovered.status == PromotionStatus.COMPLETED
    assert len(completion_events) == 1
    assert json.loads(completion_events[0].details_json or "{}")["saved_bytes"] == (
        len(b"original") - len(b"enc")
    )


def test_promotion_never_deletes_original_first(tmp_path: Path) -> None:
    config, job_id, source, _encoded = _ready_job(tmp_path, output_bytes=b"encoded")

    asyncio.run(promote_job(job_id, config=config, mode=PromotionMode.REPLACE_ATOMIC))

    assert source.exists()
    assert source.read_bytes() == b"encoded"
    assert not source.with_name(source.name + ".avarch-original").exists()


def test_promotion_fails_if_original_changed(tmp_path: Path) -> None:
    config, job_id, source, _encoded = _ready_job(tmp_path, output_bytes=b"encoded")
    source.write_bytes(b"changed original")

    result = asyncio.run(promote_job(job_id, config=config, mode=PromotionMode.REPLACE_ATOMIC))

    assert result.promoted is False
    assert "Source fingerprint" in (result.error_message or "")
    assert source.read_bytes() == b"changed original"


def test_promotion_fails_if_encoded_missing(tmp_path: Path) -> None:
    config, job_id, source, encoded = _ready_job(tmp_path, output_bytes=b"encoded")
    encoded.unlink()

    result = asyncio.run(promote_job(job_id, config=config, mode=PromotionMode.REPLACE_ATOMIC))

    assert result.promoted is False
    assert "Validated output" in (result.error_message or "")
    assert source.read_bytes() == b"original"


def test_promotion_is_retry_safe_after_partial_move(tmp_path: Path) -> None:
    config, job_id, source, encoded = _ready_job(tmp_path, output_bytes=b"encoded")
    backup = source.with_name(source.name + ".avarch-original")
    backup.write_bytes(source.read_bytes())

    result = asyncio.run(promote_job(job_id, config=config, mode=PromotionMode.REPLACE_ATOMIC))

    assert result.promoted is False
    assert source.read_bytes() == b"original"
    assert encoded.read_bytes() == b"encoded"


def test_two_jobs_cannot_promote_same_target(tmp_path: Path) -> None:
    config, job_id, source, _encoded = _ready_job(tmp_path, output_bytes=b"encoded")
    engine = create_db_engine(config.database.url)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        _insert_active_target_lock(session, target=source, now=now)
        try:
            claim_promotion(
                session,
                job_id=job_id,
                mode=PromotionMode.REPLACE_ATOMIC,
                owner_token="owner",
                now=now,
            )
        except Exception as exc:
            error = exc
        else:
            error = None

    assert error is not None
    assert "already locked" in str(error)


def test_failed_promotion_releases_lock(tmp_path: Path) -> None:
    config, job_id, _source, _encoded = _ready_job(tmp_path, output_bytes=b"encoded")
    engine = create_db_engine(config.database.url)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        record = claim_promotion(
            session,
            job_id=job_id,
            mode=PromotionMode.REPLACE_ATOMIC,
            owner_token="owner",
            now=now,
        )
        promotion_id = record.id or 0
        mark_promotion_failed_or_validated(
            session,
            promotion_id=promotion_id,
            error=RuntimeError("promotion failed"),
            now=now,
        )

    with Session(engine) as session:
        record = session.get(PromotionRecord, promotion_id)
        event = session.exec(
            select(JobEvent).where(
                JobEvent.job_id == job_id,
                JobEvent.event_type == JobEventType.STAGE_FAILED,
            )
        ).one()

    assert record is not None
    assert record.owner_token is None
    assert record.lease_expires_at is None
    assert record.status == PromotionStatus.FAILED
    failure_details = json.loads(event.details_json or "{}")
    assert failure_details["promotion_id"] == promotion_id
    assert failure_details["error_type"] == "RuntimeError"


def test_promoted_job_keeps_terminal_state(tmp_path: Path) -> None:
    config, job_id, _source, _encoded = _ready_job(tmp_path, output_bytes=b"encoded")
    asyncio.run(promote_job(job_id, config=config, mode=PromotionMode.REPLACE_ATOMIC))
    engine = create_db_engine(config.database.url)

    with Session(engine) as session, session.begin():
        job = session.get(Job, job_id)
        assert job is not None
        assert job.status == JobStatus.PROMOTED
        assert job.outcome_reason == JobOutcomeReason.SUCCESS
        try:
            transition_job(job, JobStatus.READY_TO_PROMOTE)
        except JobTransitionError as exc:
            error = exc
        else:
            error = None

    assert error is not None


def _insert_active_target_lock(session: Session, *, target: Path, now: datetime) -> None:
    job = Job(
        media_file_id=1,
        profile_name="av1_1080p_sdr",
        profile_hash="profile-hash",
        source_fs_fingerprint="other-fingerprint",
        queue_key=f"lock-{now.timestamp()}",
        plan_hash="lock-plan",
        status=JobStatus.PROMOTING,
        stage=JobStage.PROMOTE,
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    session.flush()
    attempt = JobAttempt(
        job_id=job.id or 0,
        attempt_number=1,
        stage=JobStage.PROMOTE,
        resource_class=ResourceClass.FILE_OP,
        status=AttemptStatus.RUNNING,
        runner_id="other-owner",
        started_at=now,
    )
    session.add(attempt)
    session.flush()
    validation = ValidationResult(
        job_id=job.id or 0,
        attempt_id=attempt.id or 0,
        plan_hash="lock-plan",
        policy_hash="policy-hash",
        output_path=str(target.with_name("other-output.mkv")),
        output_fs_fingerprint="output-fingerprint",
        passed=True,
        details_json="{}",
        created_at=now,
    )
    session.add(validation)
    session.flush()
    session.add(
        PromotionRecord(
            operation_id="locked-target",
            job_id=job.id or 0,
            attempt_id=attempt.id or 0,
            validation_result_id=validation.id or 0,
            mode=PromotionMode.REPLACE_ATOMIC,
            status=PromotionStatus.RUNNING,
            phase=PromotionPhase.PREPARED,
            source_path=str(target.with_name("other-source.mkv")),
            validated_output_path=str(target.with_name("other-output.mkv")),
            final_path=str(target),
            promotion_target_path=str(target),
            staging_path=str(target.with_name(".other.tmp")),
            backup_path=str(target.with_name("other.backup")),
            source_fingerprint_before="source-fingerprint",
            source_stat_json="{}",
            validated_output_fingerprint="output-fingerprint",
            validated_output_digest=None,
            staging_digest=None,
            final_fingerprint=None,
            final_digest=None,
            journal_path=str(target.with_name("journal.json")),
            owner_token="other-owner",
            heartbeat_at=now,
            lease_expires_at=now + timedelta(seconds=30),
            created_at=now,
            updated_at=now,
            started_at=now,
        )
    )
