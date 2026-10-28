"""Shared byte-fixture jobs; these are not evidence of valid media."""

from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session

from avarch.adapters.filesystem.scanner import create_file_snapshot
from avarch.adapters.sqlite.db import create_db_engine, create_db_schema
from avarch.adapters.sqlite.models import (
    Job,
    JobAttempt,
    MediaFile,
    MediaFileStatus,
    ValidationResult,
)
from avarch.adapters.sqlite.promotion_approvals import approve_job_promotion
from avarch.config import AppConfig, DatabaseSettings
from avarch.domain.jobs import AttemptStatus, JobStage, JobStatus, ResourceClass
from avarch.models.promotion import PromotionMode
from avarch.serialization import canonical_json
from tests.test_plan_models import sample_plan


def fake_replacement_decode(*_args: object, **_kwargs: object) -> dict[str, object]:
    return {"test_only": True}


def ready_job(
    tmp_path: Path,
    *,
    output_bytes: bytes,
) -> tuple[AppConfig, int, Path, Path]:
    database_path = tmp_path / "avarch.adapters.sqlite.db"
    config = AppConfig(database=DatabaseSettings(url=f"sqlite:///{database_path}"))
    engine = create_db_engine(config.database.url)
    create_db_schema(engine)
    now = datetime.now(UTC)
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"original")
    source_snapshot = create_file_snapshot(source)
    work_dir = tmp_path / "work"
    runtime_dir = work_dir / "runtime"
    artifact_dir = tmp_path / "plans"
    encoded = work_dir / "movie.av1.mkv"
    encoded.parent.mkdir(parents=True, exist_ok=True)
    encoded.write_bytes(output_bytes)
    base = sample_plan()
    plan = base.model_copy(
        update={
            "input_path": source,
            "output_path": encoded,
            "temp_dir": work_dir,
            "source_fs_fingerprint": source_snapshot.fs_fingerprint,
            "av1an": base.av1an.model_copy(
                update={
                    "video_output_path": work_dir / "video-only.mkv",
                    "temp_dir": work_dir / "av1an",
                    "working_directory": work_dir,
                }
            ),
            "mux": base.mux.model_copy(
                update={
                    "video_input_path": work_dir / "video-only.mkv",
                    "source_input_path": source,
                    "output_path": encoded,
                }
            ),
            "runtime": base.runtime.model_copy(
                update={
                    "runtime_dir": runtime_dir,
                    "av1an_stdout_log": runtime_dir / "av1an.stdout.log",
                    "av1an_stderr_log": runtime_dir / "av1an.stderr.log",
                    "mux_stdout_log": runtime_dir / "mux.stdout.log",
                    "mux_stderr_log": runtime_dir / "mux.stderr.log",
                    "av1an_stage_marker": runtime_dir / "av1an-stage.json",
                    "encode_result": runtime_dir / "encode-result.json",
                    "validation_report": runtime_dir / "validation-report.json",
                    "validation_decode_stdout_log": runtime_dir / "validation.decode.stdout.log",
                    "validation_decode_stderr_log": runtime_dir / "validation.decode.stderr.log",
                }
            ),
            "artifacts": base.artifacts.model_copy(
                update={
                    "artifact_dir": artifact_dir,
                    "plan_json": artifact_dir / "plan.json",
                    "vapoursynth_script": artifact_dir / "movie.vpy",
                    "av1an_command_json": artifact_dir / "av1an.command.json",
                    "validation_policy_json": artifact_dir / "validation-policy.json",
                }
            ),
        }
    )
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (artifact_dir / "plan.json").write_text(canonical_json(plan) + "\n", encoding="utf-8")

    with Session(engine) as session, session.begin():
        media_file = MediaFile(
            path=str(source),
            size_bytes=source_snapshot.size_bytes,
            mtime_ns=source_snapshot.mtime_ns,
            device_id=source_snapshot.device_id,
            inode=source_snapshot.inode,
            fs_fingerprint=source_snapshot.fs_fingerprint,
            discovered_at=now,
            last_seen_at=now,
            status=MediaFileStatus.PRESENT,
        )
        session.add(media_file)
        session.flush()
        job = Job(
            media_file_id=media_file.id or 0,
            profile_name="av1_1080p_sdr",
            profile_hash="profile-hash",
            source_fs_fingerprint=source_snapshot.fs_fingerprint,
            queue_key=f"queue-{now.timestamp()}",
            plan_hash=plan.plan_hash,
            plan_path=str(plan.artifacts.plan_json),
            output_path=str(encoded),
            status=JobStatus.READY_TO_PROMOTE,
            stage=JobStage.PROMOTE,
            attempts=1,
            created_at=now,
            updated_at=now,
        )
        session.add(job)
        session.flush()
        attempt = JobAttempt(
            job_id=job.id or 0,
            attempt_number=1,
            stage=JobStage.VALIDATE,
            resource_class=ResourceClass.CHEAP,
            status=AttemptStatus.COMPLETED,
            runner_id="runner",
            started_at=now,
            finished_at=now,
        )
        session.add(attempt)
        session.flush()
        validation = ValidationResult(
            job_id=job.id or 0,
            attempt_id=attempt.id or 0,
            plan_hash=plan.plan_hash,
            policy_hash=plan.validation.policy_hash,
            output_path=str(encoded),
            output_fs_fingerprint=create_file_snapshot(encoded).fs_fingerprint,
            passed=True,
            details_json="{}",
            created_at=now,
        )
        session.add(validation)
        session.flush()
        job.latest_validation_id = validation.id
        approve_job_promotion(
            session,
            job=job,
            mode=PromotionMode.REPLACE_ATOMIC,
            actor="test",
            now=now,
            delete_backup=True,
        )
        session.add(job)
        job_id = job.id or 0

    return config, job_id, source, encoded
