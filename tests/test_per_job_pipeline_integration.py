from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session

from avarch.adapters.sqlite.db import create_db_engine, create_db_schema
from avarch.adapters.sqlite.job_transitions import recover_abandoned_jobs
from avarch.adapters.sqlite.models import Job, MediaFile, MediaFileStatus
from avarch.adapters.sqlite.queue import claimable_jobs
from avarch.adapters.sqlite.rejection_cleanup import cleanup_rejected_output
from avarch.domain.jobs import JobOutcomeReason, JobStage, JobStatus
from avarch.domain.scheduler import ResourceCapacity, has_resource_capacity
from avarch.domain.size import SizeDecision, SizePolicy, evaluate_size_policy
from avarch.profiles.models import (
    EncodingProfile,
    ProfileAudioSettings,
    ProfileAv1anSettings,
    ProfileMatchSettings,
    ProfileSubtitleSettings,
    ProfileVideoSettings,
)


def test_batch_with_success_failure_and_size_rejection(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        media_file = _media_file(tmp_path / "movie.mkv", now)
        session.add(media_file)
        session.flush()
        session.add(
            _job(
                media_file.id or 0,
                now,
                queue_key="success",
                status=JobStatus.PROMOTED,
                stage=JobStage.PROMOTE,
                outcome_reason=JobOutcomeReason.SUCCESS,
            )
        )
        session.add(
            _job(
                media_file.id or 0,
                now,
                queue_key="validation-failed",
                status=JobStatus.VALIDATION_FAILED,
                stage=JobStage.VALIDATE,
                last_error_message="ffprobe could not read output",
            )
        )
        session.add(
            _job(
                media_file.id or 0,
                now,
                queue_key="size-rejected",
                status=JobStatus.SIZE_REJECTED,
                stage=JobStage.VALIDATE,
                outcome_reason=JobOutcomeReason.SKIPPED_SIZE_NOT_SMALLER,
            )
        )
        session.add(_job(media_file.id or 0, now, queue_key="unrelated"))

    with Session(engine) as session:
        claimable = claimable_jobs(session, active_job_ids=set())

    assert [job.queue_key for job in claimable] == ["unrelated"]


def test_larger_output_is_deleted(tmp_path: Path) -> None:
    original = tmp_path / "movie.mkv"
    encoded = tmp_path / "movie.av1.mkv"
    original.write_bytes(b"small")
    encoded.write_bytes(b"larger output")
    job = _job(1, datetime.now(UTC), queue_key="size", status=JobStatus.VALIDATING)
    profile = _profile()
    decision = evaluate_size_policy(
        original.stat().st_size,
        encoded.stat().st_size,
        SizePolicy(
            require_smaller=profile.promotion.require_smaller,
            minimum_savings_percent=profile.promotion.minimum_savings_percent,
        ),
    )

    cleanup_rejected_output(
        job,
        encoded_path=encoded,
        decision=decision,
        profile=profile,
        now=datetime.now(UTC),
    )

    assert decision == SizeDecision.REJECT_NOT_SMALLER
    assert not encoded.exists()
    assert original.read_bytes() == b"small"


def test_promotion_happens_before_all_encodes_finish(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        media_file = _media_file(tmp_path / "movie.mkv", now)
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

    with Session(engine) as session:
        promote_job = claimable_jobs(session, active_job_ids=set())[0]

    assert has_resource_capacity(
        promote_job.stage,
        [JobStage.ENCODE],
        capacity=ResourceCapacity(cheap_workers=4, av1an_jobs=1, file_ops=1),
    )


def test_scheduler_restart_mid_promotion(tmp_path: Path) -> None:
    backup = tmp_path / "movie.mkv.avarch-original"
    backup.write_bytes(b"original")
    engine = _engine(tmp_path)
    now = datetime.now(UTC)
    with Session(engine) as session, session.begin():
        media_file = _media_file(tmp_path / "movie.mkv", now)
        session.add(media_file)
        session.flush()
        session.add(
            _job(
                media_file.id or 0,
                now,
                queue_key="promoting",
                status=JobStatus.PROMOTING,
                stage=JobStage.PROMOTE,
            )
        )

    with Session(engine) as session, session.begin():
        summary = recover_abandoned_jobs(
            session,
            now=now,
            encoded_output_exists=_encoded_output_exists,
        )

    assert summary.recovered_jobs == 1
    assert backup.read_bytes() == b"original"


def _engine(tmp_path: Path):
    engine = create_db_engine(f"sqlite:///{tmp_path / 'avarch.db'}")
    create_db_schema(engine)
    return engine


def _encoded_output_exists(job: Job) -> bool:
    return job.output_path is not None and Path(job.output_path).exists()


def _media_file(path: Path, now: datetime) -> MediaFile:
    return MediaFile(
        path=str(path),
        size_bytes=1,
        mtime_ns=2,
        device_id=3,
        inode=4,
        fs_fingerprint=f"fingerprint:{path.name}",
        status=MediaFileStatus.PRESENT,
    )


def _job(
    media_file_id: int,
    now: datetime,
    *,
    queue_key: str,
    status: JobStatus = JobStatus.QUEUED,
    stage: JobStage = JobStage.ENCODE,
    outcome_reason: JobOutcomeReason | None = None,
    last_error_message: str | None = None,
) -> Job:
    return Job(
        media_file_id=media_file_id,
        profile_name="av1_1080p_sdr",
        profile_hash="profile-hash",
        source_fs_fingerprint="fingerprint",
        queue_key=queue_key,
        status=status,
        stage=stage,
        outcome_reason=outcome_reason,
        last_error_message=last_error_message,
        created_at=now,
        updated_at=now,
    )


def _profile() -> EncodingProfile:
    return EncodingProfile(
        backend="av1an",
        container="mkv",
        match=ProfileMatchSettings(video_codec_not=["av1"]),
        video=ProfileVideoSettings(max_width=1920),
        av1an=ProfileAv1anSettings(encoder="svt-av1", workers=1, video_args="--crf 30"),
        audio=ProfileAudioSettings(codec="opus", bitrate="128k", channels=2, languages=["eng"]),
        subtitles=ProfileSubtitleSettings(languages=["eng"]),
    )
