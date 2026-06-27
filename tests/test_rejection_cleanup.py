from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from avarch.adapters.sqlite.job_transitions import JobTransitionError, transition_job
from avarch.adapters.sqlite.models import Job
from avarch.adapters.sqlite.rejection_cleanup import (
    RejectedOutputCleanupError,
    cleanup_rejected_output,
)
from avarch.domain.jobs import JobOutcomeReason, JobStage, JobStatus
from avarch.domain.size import SizeDecision
from avarch.profiles.models import (
    EncodingProfile,
    ProfileAudioSettings,
    ProfileAv1anSettings,
    ProfileMatchSettings,
    ProfileSubtitleSettings,
    ProfileVideoSettings,
)


def test_larger_encoded_file_is_deleted(tmp_path: Path) -> None:
    original = tmp_path / "movie.mkv"
    encoded = tmp_path / "movie.av1.mkv"
    original.write_bytes(b"original")
    encoded.write_bytes(b"encoded-output-is-larger")
    job = _job()

    cleanup_rejected_output(
        job,
        encoded_path=encoded,
        decision=SizeDecision.REJECT_NOT_SMALLER,
        profile=_profile(),
        now=datetime.now(UTC),
    )

    assert not encoded.exists()
    assert job.status == JobStatus.SIZE_REJECTED
    assert job.outcome_reason == JobOutcomeReason.SKIPPED_SIZE_NOT_SMALLER


def test_original_file_remains_after_size_rejection(tmp_path: Path) -> None:
    original = tmp_path / "movie.mkv"
    encoded = tmp_path / "movie.av1.mkv"
    original.write_bytes(b"original")
    encoded.write_bytes(b"encoded-output-is-larger")
    job = _job()

    cleanup_rejected_output(
        job,
        encoded_path=encoded,
        decision=SizeDecision.REJECT_NOT_SMALLER,
        profile=_profile(),
        now=datetime.now(UTC),
    )

    assert original.read_bytes() == b"original"


def test_size_rejected_job_is_terminal(tmp_path: Path) -> None:
    encoded = tmp_path / "movie.av1.mkv"
    encoded.write_bytes(b"encoded-output-is-larger")
    job = _job()

    cleanup_rejected_output(
        job,
        encoded_path=encoded,
        decision=SizeDecision.REJECT_NOT_SMALLER,
        profile=_profile(),
        now=datetime.now(UTC),
    )

    with pytest.raises(JobTransitionError):
        transition_job(job, JobStatus.PROMOTING)


def test_cleanup_failure_marks_job_failed_without_touching_original(tmp_path: Path) -> None:
    original = tmp_path / "movie.mkv"
    encoded = tmp_path / "movie.av1.mkv"
    original.write_bytes(b"original")
    encoded.mkdir()
    job = _job()

    with pytest.raises(RejectedOutputCleanupError):
        cleanup_rejected_output(
            job,
            encoded_path=encoded,
            decision=SizeDecision.REJECT_NOT_SMALLER,
            profile=_profile(),
            now=datetime.now(UTC),
        )

    assert original.read_bytes() == b"original"
    assert job.status == JobStatus.FAILED
    assert job.last_error_type == "IsADirectoryError"


def _job() -> Job:
    now = datetime.now(UTC)
    return Job(
        media_file_id=1,
        profile_name="av1_1080p_sdr",
        profile_hash="profile-hash",
        source_fs_fingerprint="fingerprint",
        queue_key="queue-key",
        status=JobStatus.VALIDATING,
        stage=JobStage.VALIDATE,
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
