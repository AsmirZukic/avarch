from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlmodel import Session
from typer.testing import CliRunner

from avarch.cli import app
from avarch.config import load_config, resolve_database_url
from avarch.db import create_db_engine
from avarch.models.db import Job, JobAttempt, MediaFile, MediaFileStatus, MediaPlan, ProbeResult
from avarch.models.scheduler import AttemptStatus, JobStage, JobStatus, ResourceClass
from avarch.scheduler import SchedulerRunSummary

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
    assert "pending" in result.output
    assert "movie.mkv" in result.output


def test_jobs_list_prints_table_without_internal_info_logs(tmp_path: Path) -> None:
    _init_config(tmp_path)

    result = runner.invoke(app, ["jobs", "list"])

    assert result.exit_code == 0
    assert result.output.startswith("ID  STATUS")
    assert "config_loaded" not in result.output
    assert "alembic.runtime.migration" not in result.output


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

    async def fake_run_scheduler(**_kwargs: object) -> SchedulerRunSummary:
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

    async def fake_run_scheduler(**_kwargs: object) -> SchedulerRunSummary:
        return SchedulerRunSummary(completed=0, failed=1, skipped=0, idle=True)

    monkeypatch.setattr("avarch.cli.run_scheduler", fake_run_scheduler)

    result = runner.invoke(app, ["scheduler", "run"])

    assert result.exit_code == 0
    assert "Failed jobs:" in result.output
    assert "ToolUnavailableError:" in result.output
    assert "libvsscript.so" in result.output
    assert "Run vapoursynth config" in result.output
    assert f"stderr: {stderr_log} (missing)" in result.output


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
