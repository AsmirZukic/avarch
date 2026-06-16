from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlmodel import Session, col, select

from avarch.config import DEFAULT_CONFIG_TEXT, AppConfig, load_config
from avarch.db import create_db_engine, create_db_schema
from avarch.models.db import (
    Job,
    JobEvent,
    MediaFile,
    MediaFileStatus,
    PromotionRecord,
    ValidationResult,
)
from avarch.models.plan import TranscodePlan
from avarch.models.promotion import PromotionMode, PromotionPhase, PromotionStatus
from avarch.models.scheduler import JobEventType, JobStage, JobStatus
from avarch.planner import build_profile_hash
from avarch.probe import normalize_probe, store_probe_result
from avarch.profiles.registry import ProfileRegistry
from avarch.scanner import create_file_snapshot
from avarch.scheduler import JobControlError, clear_queue, retry_job, retry_queue
from avarch.serialization import canonical_json
from tests.probe_fixtures import sdr_probe_payload
from tests.test_plan_models import sample_plan


def test_queue_clear_requires_at_least_one_selector(tmp_path: Path) -> None:
    engine = _engine(tmp_path)

    with Session(engine) as session, session.begin(), pytest.raises(JobControlError):
        clear_queue(session, actor="test", now=datetime.now(UTC))


def test_queue_clear_preview_does_not_mutate_jobs(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        pending = _store_job(session, tmp_path, "pending", status=JobStatus.PENDING, now=now)
        held = _store_job(session, tmp_path, "held", status=JobStatus.HELD, now=now)
        pending_id = pending.id or 0
        held_id = held.id or 0

    with Session(engine) as session, session.begin():
        summary = clear_queue(
            session,
            actor="test",
            now=now,
            statuses={JobStatus.PENDING, JobStatus.HELD},
            confirm=False,
        )

    with Session(engine) as session:
        assert _get_job(session, pending_id).status == JobStatus.PENDING
        assert _get_job(session, held_id).status == JobStatus.HELD
        assert session.exec(select(JobEvent)).all() == []
    assert summary.matched == 2
    assert summary.immediate_cancel == 2
    assert summary.changed == 0


def test_queue_clear_confirm_cancels_eligible_nonrunning_jobs(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        ids = [
            _store_job(session, tmp_path, "pending", status=JobStatus.PENDING, now=now).id,
            _store_job(session, tmp_path, "held", status=JobStatus.HELD, now=now).id,
            _store_job(session, tmp_path, "failed", status=JobStatus.FAILED, now=now).id,
            _store_job(session, tmp_path, "validated", status=JobStatus.VALIDATED, now=now).id,
        ]

    with Session(engine) as session, session.begin():
        summary = clear_queue(
            session,
            actor="test",
            now=now,
            statuses={
                JobStatus.PENDING,
                JobStatus.HELD,
                JobStatus.FAILED,
                JobStatus.VALIDATED,
            },
            confirm=True,
        )

    with Session(engine) as session:
        statuses = [_get_job(session, job_id).status for job_id in ids if job_id is not None]
        events = session.exec(select(JobEvent).order_by(col(JobEvent.id))).all()

    assert summary.changed == 4
    assert statuses == [JobStatus.CANCELED] * 4
    assert [event.event_type for event in events] == [JobEventType.QUEUE_CLEARED] * 4
    assert all(event.details_json is not None for event in events)


def test_queue_clear_running_requires_cancel_running(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        running = _store_job(session, tmp_path, "running", status=JobStatus.RUNNING, now=now)
        running_id = running.id or 0

    with Session(engine) as session, session.begin():
        preview = clear_queue(
            session,
            actor="test",
            now=now,
            statuses={JobStatus.RUNNING},
            confirm=True,
            cancel_running=False,
        )
    with Session(engine) as session:
        assert _get_job(session, running_id).status == JobStatus.RUNNING

    with Session(engine) as session, session.begin():
        requested = clear_queue(
            session,
            actor="test",
            now=now,
            statuses={JobStatus.RUNNING},
            confirm=True,
            cancel_running=True,
        )

    with Session(engine) as session:
        stored = _get_job(session, running_id)

    assert preview.running_requests == 0
    assert requested.running_requests == 1
    assert stored.status == JobStatus.RUNNING
    assert stored.cancel_requested_at == now.replace(tzinfo=None)


def test_queue_clear_excludes_running_promotion_and_terminal_jobs(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        promotion = _store_job(
            session,
            tmp_path,
            "promotion",
            status=JobStatus.RUNNING,
            stage=JobStage.PROMOTE,
            now=now,
        )
        completed = _store_job(
            session,
            tmp_path,
            "completed",
            status=JobStatus.COMPLETED,
            now=now,
        )
        skipped = _store_job(session, tmp_path, "skipped", status=JobStatus.SKIPPED, now=now)
        promotion_id = promotion.id or 0
        completed_id = completed.id or 0
        skipped_id = skipped.id or 0

    with Session(engine) as session, session.begin():
        summary = clear_queue(
            session,
            actor="test",
            now=now,
            all_jobs=True,
            cancel_running=True,
            confirm=True,
        )

    with Session(engine) as session:
        assert _get_job(session, promotion_id).status == JobStatus.RUNNING
        assert _get_job(session, completed_id).status == JobStatus.COMPLETED
        assert _get_job(session, skipped_id).status == JobStatus.SKIPPED
    assert summary.promotion_excluded == 1
    assert summary.completed_excluded == 0
    assert summary.changed == 0


def test_queue_retry_preview_does_not_mutate_retryable_job(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    config = _config(tmp_path)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        job = _retry_job(session, tmp_path, config=config, state="plan", now=now)
        job_id = job.id or 0

    with Session(engine) as session, session.begin():
        summary = retry_queue(
            session,
            config=config,
            actor="test",
            now=now,
            statuses={JobStatus.FAILED},
            confirm=False,
        )

    with Session(engine) as session:
        stored = _get_job(session, job_id)
        events = session.exec(select(JobEvent)).all()

    assert summary.retryable == 1
    assert summary.reset_to_plan == 1
    assert stored.status == JobStatus.FAILED
    assert events == []


@pytest.mark.parametrize(
    "state,expected_stage,summary_field",
    [
        ("missing_probe", JobStage.PROBE, "reset_to_probe"),
        ("plan", JobStage.PLAN, "reset_to_plan"),
        ("encode", JobStage.ENCODE, "reset_to_encode"),
        ("validate", JobStage.VALIDATE, "reset_to_validate"),
        ("promote", JobStage.PROMOTE, "return_to_promote"),
    ],
)
def test_queue_retry_resolves_safe_resume_stage(
    tmp_path: Path,
    state: str,
    expected_stage: JobStage,
    summary_field: str,
) -> None:
    engine = _engine(tmp_path)
    config = _config(tmp_path)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        job = _retry_job(session, tmp_path, config=config, state=state, now=now)
        job_id = job.id or 0

    with Session(engine) as session, session.begin():
        summary = retry_queue(
            session,
            config=config,
            actor="test",
            now=now,
            statuses={JobStatus.FAILED},
            confirm=True,
        )

    with Session(engine) as session:
        stored = _get_job(session, job_id)
        events = session.exec(select(JobEvent).where(JobEvent.job_id == job_id)).all()

    assert getattr(summary, summary_field) == 1
    assert stored.stage == expected_stage
    assert stored.status == (
        JobStatus.VALIDATED if expected_stage == JobStage.PROMOTE else JobStatus.PENDING
    )
    assert [event.event_type for event in events] == [JobEventType.RETRY_REQUESTED]


def test_queue_retry_reports_reenqueue_when_source_identity_changed(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    config = _config(tmp_path)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        job = _retry_job(session, tmp_path, config=config, state="plan", now=now)
        job.source_fs_fingerprint = "old-fingerprint"
        job_id = job.id or 0
        session.add(job)

    with Session(engine) as session, session.begin():
        summary = retry_queue(
            session,
            config=config,
            actor="test",
            now=now,
            statuses={JobStatus.FAILED},
            confirm=True,
        )

    with Session(engine) as session:
        stored = _get_job(session, job_id)

    assert summary.retryable == 0
    assert summary.requires_requeue == 1
    assert stored.status == JobStatus.FAILED


def test_retry_job_rejects_completed_promotion(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    config = _config(tmp_path)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        job = _retry_job(session, tmp_path, config=config, state="promote", now=now)
        job_id = job.id or 0
        _store_completed_promotion(session, job_id=job_id, now=now)

    with Session(engine) as session, session.begin(), pytest.raises(JobControlError):
        retry_job(
            session,
            job_id=job_id,
            config=config,
            actor="test",
            now=now,
        )


def _retry_job(
    session: Session,
    tmp_path: Path,
    *,
    config: AppConfig,
    state: str,
    now: datetime,
) -> Job:
    media_file = _store_media_file(session, tmp_path / f"{state}.mkv", now)
    profile = ProfileRegistry.from_config(config).get("av1_1080p_sdr")
    profile_hash = build_profile_hash(profile.profile)
    probe_hash: str | None = None
    probe_result_id: int | None = None
    if state != "missing_probe":
        raw_probe = sdr_probe_payload()
        probe = store_probe_result(
            session,
            media_file=media_file,
            raw_probe=raw_probe,
            normalized_probe=normalize_probe(raw_probe),
            created_at=now,
        )
        session.flush()
        probe_hash = probe.probe_hash
        probe_result_id = probe.id

    plan = _localized_plan(tmp_path=tmp_path, state=state)
    plan_hash: str | None = None
    plan_path: str | None = None
    output_path: str | None = None
    if state in {"encode", "validate", "promote"}:
        _write_plan(plan)
        plan_hash = plan.plan_hash
        plan_path = str(plan.artifacts.plan_json)
        output_path = str(plan.output_path)
    if state in {"validate", "promote"}:
        plan.output_path.parent.mkdir(parents=True, exist_ok=True)
        plan.output_path.write_bytes(b"encoded output")

    job = Job(
        media_file_id=media_file.id or 0,
        profile_name="av1_1080p_sdr",
        profile_hash=profile_hash,
        source_fs_fingerprint=media_file.fs_fingerprint,
        queue_key=f"{state}-queue",
        probe_result_id=probe_result_id,
        probe_hash=probe_hash,
        plan_hash=plan_hash,
        plan_path=plan_path,
        output_path=output_path,
        status=JobStatus.FAILED,
        stage=JobStage.VALIDATE,
        last_error_type="Error",
        last_error_message="failed",
        created_at=now,
        updated_at=now,
        finished_at=now,
    )
    session.add(job)
    session.flush()
    if state == "promote":
        validation = ValidationResult(
            job_id=job.id or 0,
            attempt_id=_dummy_attempt_id(session, job_id=job.id or 0, now=now),
            plan_hash=plan.plan_hash,
            policy_hash=plan.validation.policy_hash,
            output_path=str(plan.output_path),
            output_fs_fingerprint=create_file_snapshot(plan.output_path).fs_fingerprint,
            passed=True,
            details_json="{}",
            created_at=now,
        )
        session.add(validation)
        session.flush()
        job.latest_validation_id = validation.id
        session.add(job)
    return job


def _store_media_file(session: Session, path: Path, now: datetime) -> MediaFile:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"media")
    snapshot = create_file_snapshot(path)
    media_file = MediaFile(
        path=str(snapshot.path),
        size_bytes=snapshot.size_bytes,
        mtime_ns=snapshot.mtime_ns,
        device_id=snapshot.device_id,
        inode=snapshot.inode,
        fs_fingerprint=snapshot.fs_fingerprint,
        discovered_at=now,
        last_seen_at=now,
        status=MediaFileStatus.PRESENT,
    )
    session.add(media_file)
    session.flush()
    return media_file


def _store_job(
    session: Session,
    tmp_path: Path,
    name: str,
    *,
    status: JobStatus,
    now: datetime,
    stage: JobStage = JobStage.PROBE,
) -> Job:
    media_file = _store_media_file(session, tmp_path / f"{name}.mkv", now)
    job = Job(
        media_file_id=media_file.id or 0,
        profile_name=name,
        profile_hash="profile-hash",
        source_fs_fingerprint=media_file.fs_fingerprint,
        queue_key=f"{name}-queue",
        status=status,
        stage=stage,
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    session.flush()
    return job


def _dummy_attempt_id(
    session: Session,
    *,
    job_id: int,
    now: datetime,
    attempt_number: int = 1,
) -> int:
    from avarch.models.db import JobAttempt
    from avarch.models.scheduler import AttemptStatus, ResourceClass

    attempt = JobAttempt(
        job_id=job_id,
        attempt_number=attempt_number,
        stage=JobStage.VALIDATE,
        resource_class=ResourceClass.CHEAP,
        status=AttemptStatus.COMPLETED,
        runner_id="runner",
        started_at=now,
        finished_at=now,
    )
    session.add(attempt)
    session.flush()
    return attempt.id or 0


def _store_completed_promotion(session: Session, *, job_id: int, now: datetime) -> None:
    attempt_id = _dummy_attempt_id(session, job_id=job_id, now=now, attempt_number=2)
    validation = session.exec(
        select(ValidationResult).where(ValidationResult.job_id == job_id)
    ).one()
    session.add(
        PromotionRecord(
            operation_id="operation",
            job_id=job_id,
            attempt_id=attempt_id,
            validation_result_id=validation.id or 0,
            mode=PromotionMode.KEEP_ORIGINAL,
            status=PromotionStatus.COMPLETED,
            phase=PromotionPhase.COMMITTED,
            source_path="/media/movie.mkv",
            validated_output_path="/work/movie.av1.mkv",
            final_path="/media/movie.av1.mkv",
            staging_path="/media/.movie.av1.mkv.tmp",
            backup_path=None,
            source_fingerprint_before="source-fs",
            source_stat_json="{}",
            validated_output_fingerprint="output-fs",
            validated_output_digest=None,
            staging_digest=None,
            final_fingerprint=None,
            final_digest=None,
            journal_path="/work/runtime/promotion-journal.json",
            cleanup_completed=True,
            created_at=now,
            updated_at=now,
            started_at=now,
        )
    )


def _localized_plan(*, tmp_path: Path, state: str) -> TranscodePlan:
    base = sample_plan()
    root = tmp_path / ".avarch" / state
    artifact_dir = root / "plans"
    output_path = root / "work" / "movie.av1.mkv"
    return base.model_copy(
        update={
            "output_path": output_path,
            "mux": base.mux.model_copy(update={"output_path": output_path}),
            "artifacts": base.artifacts.model_copy(
                update={
                    "artifact_dir": artifact_dir,
                    "plan_json": artifact_dir / "plan.json",
                }
            ),
        }
    )


def _write_plan(plan: TranscodePlan) -> None:
    plan.artifacts.artifact_dir.mkdir(parents=True, exist_ok=True)
    plan.artifacts.plan_json.write_text(canonical_json(plan) + "\n", encoding="utf-8")


def _get_job(session: Session, job_id: int) -> Job:
    job = session.get(Job, job_id)
    assert job is not None
    return job


def _config(tmp_path: Path) -> AppConfig:
    config_path = tmp_path / "avarch.toml"
    config_path.write_text(DEFAULT_CONFIG_TEXT, encoding="utf-8")
    return load_config(config_path)


def _engine(tmp_path: Path) -> Engine:
    tmp_path.mkdir(parents=True, exist_ok=True)
    engine = create_db_engine(f"sqlite:///{tmp_path / 'avarch.db'}")
    create_db_schema(engine)
    return engine
