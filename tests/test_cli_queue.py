from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlmodel import Session
from typer.testing import CliRunner

from avarch.adapters.sqlite.db import create_db_engine
from avarch.adapters.sqlite.models import (
    Job,
    JobAttempt,
    MediaFile,
    MediaFileStatus,
    MediaPlan,
    ProbeResult,
)
from avarch.adapters.sqlite.progress import SqliteProgressStore
from avarch.adapters.sqlite.urls import resolve_database_url
from avarch.application.scheduler_run import SchedulerRunSummary
from avarch.cli import app
from avarch.config import load_config
from avarch.domain.jobs import (
    AttemptStatus,
    JobOutcomeReason,
    JobStage,
    JobStatus,
    ResourceClass,
)
from avarch.domain.progress import ProgressPhase, ProgressSnapshot, ProgressSource, ProgressUnit

runner = CliRunner()


def test_enqueue_command_creates_jobs(tmp_path: Path) -> None:
    config_path = _init_config(tmp_path)
    _insert_current_plan(config_path, tmp_path / "movie.mkv")

    result = runner.invoke(
        app,
        ["enqueue"],
    )

    assert result.exit_code == 0
    assert "Processed: 1" in result.output


def test_jobs_command_lists_queued_jobs(tmp_path: Path) -> None:
    config_path = _init_config(tmp_path)
    _insert_current_plan(config_path, tmp_path / "movie.mkv")
    runner.invoke(app, ["enqueue"])

    result = runner.invoke(app, ["jobs", "list"])

    assert result.exit_code == 0
    assert "queued" in result.output
    assert "movie.mkv" in result.output


def test_jobs_list_prints_table_without_internal_info_logs(tmp_path: Path) -> None:
    _init_config(tmp_path)

    result = runner.invoke(app, ["jobs", "list"])

    assert result.exit_code == 0
    assert result.output.startswith("ID  STATUS")
    assert "config_loaded" not in result.output
    assert "alembic.runtime.migration" not in result.output


def test_jobs_list_shows_current_progress(tmp_path: Path) -> None:
    config_path = _init_config(tmp_path)
    _insert_job_with_progress(config_path, tmp_path / "movie-progress.mkv")

    result = runner.invoke(app, ["jobs", "list"])

    assert result.exit_code == 0
    assert "PHASE" in result.output
    assert "encoding" in result.output
    assert "40.0%" in result.output
    assert "6s" in result.output
    assert "movie-progress.mkv" in result.output


def test_jobs_show_prints_progress_details(tmp_path: Path) -> None:
    config_path = _init_config(tmp_path)
    job_id = _insert_job_with_progress(config_path, tmp_path / "movie-show-progress.mkv")

    result = runner.invoke(app, ["jobs", "show", str(job_id)])

    assert result.exit_code == 0
    assert "Progress:" in result.output
    assert "attempt:          1 (id " in result.output
    assert "phase:            encoding" in result.output
    assert "phase progress:   48 / 120 frames (40.0%)" in result.output
    assert "estimated left:   6s" in result.output
    assert "speed:            1.50x" in result.output
    assert "source:           av1an" in result.output


def test_jobs_watch_prints_plain_progress_for_redirected_output(tmp_path: Path) -> None:
    config_path = _init_config(tmp_path)
    job_id = _insert_job_with_progress(
        config_path,
        tmp_path / "movie-watch-progress.mkv",
        status=JobStatus.PROMOTED,
        phase=ProgressPhase.COMPLETED,
    )

    result = runner.invoke(app, ["jobs", "watch", str(job_id), "--poll-interval", "0.01"])

    assert result.exit_code == 0
    assert "movie-watch-progress.mkv completed 40.0%" in result.output
    assert "\x1b" not in result.output


def test_jobs_watch_exits_nonzero_for_failed_terminal_job(tmp_path: Path) -> None:
    config_path = _init_config(tmp_path)
    job_id = _insert_job_with_progress(
        config_path,
        tmp_path / "movie-watch-failed.mkv",
        status=JobStatus.FAILED,
        phase=ProgressPhase.FAILED,
    )

    result = runner.invoke(app, ["jobs", "watch", str(job_id), "--poll-interval", "0.01"])

    assert result.exit_code == 1
    assert "movie-watch-failed.mkv failed 40.0%" in result.output


def test_pause_command_sets_persistent_state(tmp_path: Path) -> None:
    _init_config(tmp_path)

    result = runner.invoke(app, ["scheduler", "pause"])

    assert result.exit_code != 0
    assert "No live scheduler process exists" in result.output


def test_queue_retry_preview_runs_without_confirm(tmp_path: Path) -> None:
    _init_config(tmp_path)

    result = runner.invoke(app, ["jobs", "retry", "--failed"])

    assert result.exit_code == 0
    assert "Failed jobs retry prepared: 0" in result.output


def test_run_command_invokes_scheduler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_config(tmp_path)
    calls: list[str] = []

    async def fake_run_scheduler(*_args: object, **_kwargs: object) -> SchedulerRunSummary:
        calls.append("called")
        return SchedulerRunSummary(completed=0, failed=0, skipped=0, idle=True)

    monkeypatch.setattr("avarch.cli.run_scheduler", fake_run_scheduler)

    result = runner.invoke(app, ["scheduler", "run"])

    assert result.exit_code == 0
    assert calls == ["called"]
    assert "Scheduler started" in result.output


def test_run_command_prints_failed_job_error_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _init_config(tmp_path)
    engine = create_db_engine(resolve_database_url(load_config(config_path), config_path))
    now = datetime.now(UTC)
    stdout_log = tmp_path / "missing-stdout.log"
    stderr_log = tmp_path / "missing-stderr.log"

    with Session(engine) as session, session.begin():
        media_file = MediaFile(
            path=str(tmp_path / "movie.mkv"),
            size_bytes=100,
            mtime_ns=1,
            device_id=2,
            inode=3,
            fs_fingerprint="fingerprint",
            status=MediaFileStatus.PRESENT,
            discovered_at=now,
            last_seen_at=now,
        )
        session.add(media_file)
        session.flush()
        job = Job(
            media_file_id=media_file.id or 0,
            profile_name="av1_1080p_sdr",
            profile_hash="profile",
            source_fs_fingerprint=media_file.fs_fingerprint,
            queue_key="queue",
            plan_hash="plan",
            output_path=str(tmp_path / "movie.av1.mkv"),
            status=JobStatus.FAILED,
            stage=JobStage.ENCODE,
            attempts=1,
            last_error_type="ToolUnavailableError",
            last_error_message=(
                "av1an --version failed with exit code 127.\n"
                "av1an: error while loading shared libraries: libvsscript.so\n\n"
                "VapourSynth diagnostic:\nRun vapoursynth config"
            ),
            created_at=now,
            updated_at=now,
            started_at=now,
            finished_at=now,
        )
        session.add(job)
        session.flush()
        session.add(
            JobAttempt(
                job_id=job.id or 0,
                attempt_number=1,
                stage=JobStage.ENCODE,
                resource_class=ResourceClass.HEAVY_AV1AN,
                status=AttemptStatus.FAILED,
                runner_id="runner",
                stdout_log=str(stdout_log),
                stderr_log=str(stderr_log),
                error_type=job.last_error_type,
                error_message=job.last_error_message,
                started_at=now,
                finished_at=now,
            )
        )

    async def fake_run_scheduler(*_args: object, **_kwargs: object) -> SchedulerRunSummary:
        return SchedulerRunSummary(completed=0, failed=1, skipped=0, idle=True)

    monkeypatch.setattr("avarch.cli.run_scheduler", fake_run_scheduler)

    result = runner.invoke(app, ["scheduler", "run"])

    assert result.exit_code == 0
    assert "Failed jobs:" in result.output
    assert "ToolUnavailableError:" in result.output
    assert "libvsscript.so" in result.output
    assert "Run vapoursynth config" in result.output
    assert f"stderr: {stderr_log} (missing)" in result.output


def test_status_shows_size_rejected_reason(tmp_path: Path) -> None:
    config_path = _init_config(tmp_path)
    _insert_job(
        config_path,
        tmp_path / "movie-d.mkv",
        status=JobStatus.SIZE_REJECTED,
        stage=JobStage.VALIDATE,
        outcome_reason=JobOutcomeReason.SKIPPED_SIZE_NOT_SMALLER,
    )

    result = runner.invoke(app, ["jobs", "list"])

    assert result.exit_code == 0
    assert "Skipped: movie-d.mkv, output was not smaller" in result.output


def test_status_shows_validation_failure_reason(tmp_path: Path) -> None:
    config_path = _init_config(tmp_path)
    _insert_job(
        config_path,
        tmp_path / "movie-e.mkv",
        status=JobStatus.VALIDATION_FAILED,
        stage=JobStage.VALIDATE,
        last_error_message="ffprobe could not read output",
    )

    result = runner.invoke(app, ["jobs", "list"])

    assert result.exit_code == 0
    assert "Failed: movie-e.mkv, ffprobe could not read output" in result.output


def test_status_shows_promoted_jobs(tmp_path: Path) -> None:
    config_path = _init_config(tmp_path)
    _insert_job(
        config_path,
        tmp_path / "movie-c.mkv",
        status=JobStatus.PROMOTED,
        stage=JobStage.PROMOTE,
        outcome_reason=JobOutcomeReason.SUCCESS,
    )

    result = runner.invoke(app, ["jobs", "list"])

    assert result.exit_code == 0
    assert "Promoted: movie-c.mkv" in result.output


def _init_config(tmp_path: Path) -> Path:
    config_path = tmp_path / ".avarch" / "config.toml"
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    return config_path


def _insert_current_plan(config_path: Path, media_path: Path) -> None:
    media_path.write_bytes(b"media")
    app_config = load_config(config_path)
    engine = create_db_engine(resolve_database_url(app_config, config_path))
    now = datetime.now(UTC)
    stat_result = media_path.stat()
    with Session(engine) as session, session.begin():
        media_file = MediaFile(
            path=str(media_path.resolve()),
            size_bytes=stat_result.st_size,
            mtime_ns=stat_result.st_mtime_ns,
            device_id=stat_result.st_dev,
            inode=stat_result.st_ino,
            fs_fingerprint="fingerprint",
            status=MediaFileStatus.PRESENT,
            discovered_at=now,
            last_seen_at=now,
        )
        session.add(media_file)
        session.flush()
        probe_result = ProbeResult(
            media_file_id=media_file.id or 0,
            ffprobe_json="{}",
            normalized_json="{}",
            probe_hash="probe",
            source_fs_fingerprint=media_file.fs_fingerprint,
            created_at=now,
        )
        session.add(probe_result)
        session.flush()
        session.add(
            MediaPlan(
                media_file_id=media_file.id or 0,
                probe_result_id=probe_result.id or 0,
                profile_name="av1_1080p_sdr",
                profile_hash="profile",
                probe_hash="probe",
                source_fs_fingerprint=media_file.fs_fingerprint,
                execution_identity_hash="execution",
                plan_hash=f"plan:{media_path.name}",
                plan_path=str(config_path.parent / "data" / "plan.json"),
                output_path=str(media_path.with_suffix(".av1.mkv")),
                is_current=True,
                is_valid=True,
                created_at=now,
            )
        )


def _insert_job(
    config_path: Path,
    media_path: Path,
    *,
    status: JobStatus,
    stage: JobStage,
    outcome_reason: JobOutcomeReason | None = None,
    last_error_message: str | None = None,
) -> None:
    media_path.write_bytes(b"media")
    app_config = load_config(config_path)
    engine = create_db_engine(resolve_database_url(app_config, config_path))
    now = datetime.now(UTC)
    stat_result = media_path.stat()
    with Session(engine) as session, session.begin():
        media_file = MediaFile(
            path=str(media_path.resolve()),
            size_bytes=stat_result.st_size,
            mtime_ns=stat_result.st_mtime_ns,
            device_id=stat_result.st_dev,
            inode=stat_result.st_ino,
            fs_fingerprint=f"fingerprint:{media_path.name}",
            status=MediaFileStatus.PRESENT,
            discovered_at=now,
            last_seen_at=now,
        )
        session.add(media_file)
        session.flush()
        session.add(
            Job(
                media_file_id=media_file.id or 0,
                profile_name="av1_1080p_sdr",
                profile_hash="profile",
                source_fs_fingerprint=media_file.fs_fingerprint,
                queue_key=f"queue:{media_path.name}",
                status=status,
                stage=stage,
                outcome_reason=outcome_reason,
                last_error_message=last_error_message,
                created_at=now,
                updated_at=now,
            )
        )


def _insert_job_with_progress(
    config_path: Path,
    media_path: Path,
    *,
    status: JobStatus = JobStatus.ENCODING,
    phase: ProgressPhase = ProgressPhase.ENCODING,
) -> int:
    media_path.write_bytes(b"media")
    app_config = load_config(config_path)
    engine = create_db_engine(resolve_database_url(app_config, config_path))
    now = datetime.now(UTC)
    stat_result = media_path.stat()
    with Session(engine) as session, session.begin():
        media_file = MediaFile(
            path=str(media_path.resolve()),
            size_bytes=stat_result.st_size,
            mtime_ns=stat_result.st_mtime_ns,
            device_id=stat_result.st_dev,
            inode=stat_result.st_ino,
            fs_fingerprint=f"fingerprint:{media_path.name}",
            status=MediaFileStatus.PRESENT,
            discovered_at=now,
            last_seen_at=now,
        )
        session.add(media_file)
        session.flush()
        job = Job(
            media_file_id=media_file.id or 0,
            profile_name="av1_1080p_sdr",
            profile_hash="profile",
            source_fs_fingerprint=media_file.fs_fingerprint,
            queue_key=f"queue:{media_path.name}",
            status=status,
            stage=JobStage.ENCODE,
            attempts=1,
            created_at=now,
            updated_at=now,
            started_at=now,
        )
        session.add(job)
        session.flush()
        attempt = JobAttempt(
            job_id=job.id or 0,
            attempt_number=1,
            stage=JobStage.ENCODE,
            resource_class=ResourceClass.HEAVY_AV1AN,
            status=(
                AttemptStatus.COMPLETED
                if status != JobStatus.ENCODING
                else AttemptStatus.RUNNING
            ),
            runner_id="runner",
            started_at=now,
        )
        session.add(attempt)
        session.flush()
        attempt_id = attempt.id
        assert attempt_id is not None
        SqliteProgressStore(session).save_snapshot(
            attempt_id=attempt_id,
            snapshot=ProgressSnapshot(
                phase=phase,
                current=48,
                total=120,
                unit=ProgressUnit.FRAMES,
                rate_per_second=12,
                speed_ratio=1.5,
                source=ProgressSource.AV1AN_OUTPUT,
                message="48/120 frames",
                phase_started_at=now - timedelta(seconds=10),
                observed_at=now,
                heartbeat_at=now,
                advanced_at=now,
            ),
            persisted_at=now,
        )
        job_id = job.id
        assert job_id is not None
        return job_id
