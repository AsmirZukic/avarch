from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import Engine
from sqlmodel import Session

from avarch.adapters.sqlite.db import create_db_engine, create_db_schema
from avarch.adapters.sqlite.models import (
    Job,
    JobAttempt,
    MediaFile,
    MediaFileStatus,
    MediaPlan,
    ProbeResult,
)
from avarch.adapters.sqlite.queue_forecast import comparable_encode_history
from avarch.domain.jobs import AttemptStatus, JobStage, JobStatus, ResourceClass
from avarch.models.probe import NormalizedProbe, VideoStream
from avarch.serialization import canonical_json


def test_history_groups_successful_encode_attempts_by_comparable_metadata(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    with Session(engine) as session:
        _history_row(
            session,
            now=now,
            profile_name="film",
            width=1920,
            height=1080,
            bit_depth=10,
            encoder="svt-av1",
            preset="6",
            workers=4,
            execution_identity_hash="vpy:stable",
            duration_seconds=3600,
        )
        session.commit()

    with Session(engine) as session:
        samples = comparable_encode_history(session)

    assert len(samples) == 1
    sample = samples[0]
    assert sample.duration_seconds == 3600
    assert sample.key.profile_name == "film"
    assert sample.key.resolution_class == "1080p"
    assert sample.key.bit_depth == 10
    assert sample.key.encoder == "svt-av1"
    assert sample.key.preset == "6"
    assert sample.key.workers == 4
    assert sample.key.vapoursynth_identity == "vpy:stable"


def test_history_excludes_failed_cancelled_incomplete_and_invalid_rows(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    with Session(engine) as session:
        _history_row(session, now=now, duration_seconds=1200)
        _history_row(session, now=now, status=AttemptStatus.FAILED, duration_seconds=1200)
        _history_row(session, now=now, status=AttemptStatus.CANCELED, duration_seconds=1200)
        _history_row(session, now=now, stage=JobStage.VALIDATE, duration_seconds=1200)
        _history_row(session, now=now, duration_seconds=None)
        _history_row(session, now=now, duration_seconds=-5)
        _history_row(session, now=now, duration_seconds=31 * 24 * 60 * 60)
        session.commit()

    with Session(engine) as session:
        samples = comparable_encode_history(session)

    assert len(samples) == 1
    assert samples[0].duration_seconds == 1200


def _engine(tmp_path: Path) -> Engine:
    engine = create_db_engine(f"sqlite:///{tmp_path / 'avarch.sqlite'}")
    create_db_schema(engine)
    return engine


def _history_row(
    session: Session,
    *,
    now: datetime,
    profile_name: str = "default",
    width: int = 1920,
    height: int = 1080,
    bit_depth: int | None = 10,
    encoder: str = "svt-av1",
    preset: str = "6",
    workers: int = 2,
    execution_identity_hash: str = "execution",
    status: AttemptStatus = AttemptStatus.COMPLETED,
    stage: JobStage = JobStage.ENCODE,
    duration_seconds: int | None,
) -> None:
    media = MediaFile(
        path=f"/media/{profile_name}-{status}-{stage}-{duration_seconds}.mkv",
        size_bytes=100,
        mtime_ns=1,
        device_id=1,
        inode=1,
        fs_fingerprint=f"fingerprint:{profile_name}:{status}:{stage}:{duration_seconds}",
        status=MediaFileStatus.PRESENT,
        discovered_at=now,
        last_seen_at=now,
    )
    session.add(media)
    session.flush()
    probe = ProbeResult(
        media_file_id=media.id or 0,
        ffprobe_json="{}",
        normalized_json=canonical_json(
            NormalizedProbe(
                video_streams=[
                    VideoStream(index=0, width=width, height=height, bit_depth=bit_depth)
                ]
            )
        ),
        probe_hash=f"probe:{media.id}",
        source_fs_fingerprint=media.fs_fingerprint,
        created_at=now,
    )
    session.add(probe)
    session.flush()
    plan_hash = f"plan:{media.id}"
    session.add(
        MediaPlan(
            media_file_id=media.id or 0,
            probe_result_id=probe.id or 0,
            profile_name=profile_name,
            profile_hash=f"profile:{profile_name}",
            probe_hash=probe.probe_hash,
            source_fs_fingerprint=media.fs_fingerprint,
            execution_identity_hash=execution_identity_hash,
            plan_hash=plan_hash,
            plan_path=f"/plans/{media.id}.json",
            output_path=f"/work/{media.id}.mkv",
            is_current=True,
            is_valid=True,
            created_at=now,
        )
    )
    job = Job(
        media_file_id=media.id or 0,
        profile_name=profile_name,
        profile_hash=f"profile:{profile_name}",
        source_fs_fingerprint=media.fs_fingerprint,
        queue_key=f"queue:{media.id}",
        probe_result_id=probe.id,
        probe_hash=probe.probe_hash,
        plan_hash=plan_hash,
        plan_path=f"/plans/{media.id}.json",
        output_path=f"/work/{media.id}.mkv",
        status=JobStatus.ENCODED,
        stage=JobStage.ENCODE,
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    session.flush()
    started_at = now - timedelta(seconds=duration_seconds) if duration_seconds is not None else now
    session.add(
        JobAttempt(
            job_id=job.id or 0,
            attempt_number=1,
            stage=stage,
            resource_class=ResourceClass.HEAVY_AV1AN,
            status=status,
            runner_id="runner",
            command_json=canonical_json(
                {
                    "av1an_argv": [
                        "av1an",
                        "--encoder",
                        encoder,
                        "--video-params",
                        f"--preset {preset} --crf 28",
                        "--workers",
                        str(workers),
                    ]
                }
            ),
            started_at=started_at,
            finished_at=now if duration_seconds is not None else None,
        )
    )
