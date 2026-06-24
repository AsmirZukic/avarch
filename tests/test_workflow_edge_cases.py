from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlmodel import Session
from typer.testing import CliRunner

from avarch.adapters.sqlite.db import create_db_engine
from avarch.adapters.sqlite.models import (
    Job,
    JobAttempt,
    MediaFile,
    MediaFileStatus,
    ProbeResult,
    ValidationResult,
)
from avarch.adapters.sqlite.probes import store_probe_result
from avarch.adapters.sqlite.urls import resolve_database_url
from avarch.cli import app
from avarch.config import AppConfig, load_config
from avarch.domain.jobs import AttemptStatus, JobStage, JobStatus, ResourceClass
from avarch.models.plan import ExecutionRuntimePaths, PlanArtifactPaths, TranscodePlan
from avarch.models.validation import (
    ObservedValidationMedia,
    ValidationCheck,
    ValidationCheckStatus,
    ValidationReport,
)
from avarch.planner import build_execution_identity, build_profile_hash, finalize_plan_hash
from avarch.probe import normalize_probe
from avarch.profiles.registry import ProfileRegistry
from avarch.scanner import create_file_snapshot
from avarch.serialization import canonical_json
from avarch.validation import validate_output
from avarch.vapoursynth import GENERATOR_VERSION, build_vapoursynth_identity_hash
from tests.probe_fixtures import sdr_probe_payload
from tests.test_plan_models import sample_plan

runner = CliRunner()


@pytest.mark.parametrize(
    "command",
    [
        [],
        ["init"],
        ["doctor"],
        ["db"],
        ["db", "current"],
        ["db", "upgrade"],
        ["scan"],
        ["files", "list"],
        ["probe"],
        ["files", "show"],
        ["plan"],
        ["enqueue"],
        ["scheduler"],
        ["scheduler", "run"],
        ["scheduler", "pause"],
        ["scheduler", "resume"],
        ["scheduler", "drain"],
        ["scheduler", "stop"],
        ["scheduler", "status"],
        ["scheduler", "restart"],
        ["jobs"],
        ["jobs", "list"],
        ["jobs", "show"],
        ["jobs", "logs"],
        ["jobs", "cancel"],
        ["jobs", "hold"],
        ["jobs", "release"],
        ["jobs", "retry"],
        ["jobs", "priority"],
        ["jobs", "clear"],
        ["jobs", "validate"],
        ["plans"],
        ["plans", "list"],
        ["plans", "show"],
    ],
)
def test_every_user_facing_command_has_help(command: list[str]) -> None:
    result = runner.invoke(app, [*command, "--help"])

    assert result.exit_code == 0
    assert "Usage:" in result.output


def test_first_time_user_can_recover_from_existing_config_with_force(tmp_path: Path) -> None:
    config_path = tmp_path / ".avarch" / "config.toml"

    first = runner.invoke(app, ["init"])
    second = runner.invoke(app, ["init"])
    forced = runner.invoke(app, ["init", "--force"])

    assert first.exit_code == 0
    assert second.exit_code != 0
    assert "Workspace already exists" in second.output
    assert forced.exit_code == 0
    assert "[profile_registry]" in config_path.read_text(encoding="utf-8")


def test_scan_workflows_report_missing_roots_file_roots_and_multiple_roots(
    tmp_path: Path,
) -> None:
    _init_config(tmp_path)
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    first_movie = first_root / "a.mkv"
    second_movie = second_root / "b.mp4"
    first_movie.write_bytes(b"a")
    second_movie.write_bytes(b"b")

    no_roots = runner.invoke(app, ["scan"])
    missing_root = runner.invoke(
        app,
        ["scan", str(tmp_path / "missing")],
    )
    file_root = runner.invoke(app, ["scan", str(first_movie)])
    multiple_roots = runner.invoke(
        app,
        ["scan", str(first_root), str(second_root)],
    )

    assert no_roots.exit_code != 0
    assert "No scan roots provided or configured." in no_roots.output
    assert missing_root.exit_code != 0
    assert "Root does not exist" in missing_root.output
    assert file_root.exit_code != 0
    assert "Root is not a directory" in file_root.output
    assert multiple_roots.exit_code == 0
    assert "Total" in multiple_roots.output
    assert "Added:     2" in multiple_roots.output


def test_novice_commands_fail_with_actionable_messages(tmp_path: Path) -> None:
    _init_config(tmp_path)
    untracked = tmp_path / "movie.mkv"
    untracked.write_bytes(b"media")

    probe_untracked = runner.invoke(app, ["probe", "--file", str(untracked)])
    inspect_untracked = runner.invoke(
        app,
        ["files", "show", "--file", str(untracked)],
    )
    plan_without_profile = runner.invoke(
        app,
        ["plan", "--file", str(untracked)],
    )
    enqueue_without_profile = runner.invoke(app, ["enqueue"])
    validate_missing_job = runner.invoke(app, ["jobs", "validate", "1"])

    assert probe_untracked.exit_code != 0
    assert "File is not present in the media inventory:" in probe_untracked.output
    assert inspect_untracked.exit_code != 0
    assert "File is not present in the media inventory." in inspect_untracked.output
    assert plan_without_profile.exit_code != 0
    assert "Missing option" in plan_without_profile.output
    assert enqueue_without_profile.exit_code == 0
    assert "Selected: 0" in enqueue_without_profile.output
    assert validate_missing_job.exit_code != 0
    assert "Job not found" in validate_missing_job.output


def test_dry_run_review_workflow_does_not_create_final_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, movie = _tracked_and_probed_movie(tmp_path, monkeypatch)

    result = runner.invoke(
        app,
        [
            "plan",
            "--profile",
            "av1_1080p_sdr",
            "--file",
            str(movie),
        ],
    )

    output_line = next(line for line in result.output.splitlines() if line.startswith("Output:"))
    output_path = Path(output_line.split(":", 1)[1].strip())
    assert result.exit_code == 0
    assert "Dry run only. No encoding was started." in result.output
    assert not output_path.exists()


def test_queue_workflows_report_profile_status_and_retry_errors(tmp_path: Path) -> None:
    _init_config(tmp_path)

    removed_profile_option = runner.invoke(
        app,
        ["enqueue", "--profile", "does_not_exist"],
    )
    invalid_status = runner.invoke(
        app,
        ["jobs", "list", "--status", "confused"],
    )
    retry_preview = runner.invoke(app, ["jobs", "retry", "--failed"])

    assert removed_profile_option.exit_code != 0
    assert "No such option" in removed_profile_option.output
    assert invalid_status.exit_code != 0
    assert "Unknown job status: confused" in invalid_status.output
    assert retry_preview.exit_code == 0
    assert "Failed jobs retry prepared: 0" in retry_preview.output


def test_jobs_workflow_shows_old_failed_job_and_new_passing_validation(
    tmp_path: Path,
) -> None:
    config_path = _init_config(tmp_path)
    engine = _engine_for_config(config_path)
    now = datetime.now(UTC)

    with Session(engine) as session, session.begin():
        media_file = _store_media_file(session, tmp_path / "movie.mkv", now)
        session.add(
            Job(
                media_file_id=media_file.id or 0,
                profile_name="av1_1080p_sdr",
                profile_hash="old-profile",
                source_fs_fingerprint=media_file.fs_fingerprint,
                queue_key="old",
                plan_hash="old-plan",
                output_path=str(tmp_path / "old-output.mkv"),
                status=JobStatus.FAILED,
                stage=JobStage.ENCODE,
                last_error_type="Av1anStageError",
                last_error_message="encoder crashed: signal: 9 (SIGKILL)",
                created_at=now,
                updated_at=now,
            )
        )
        passing_job = _store_completed_validation_job(
            session,
            tmp_path=tmp_path,
            media_file=media_file,
            now=now,
        )
        assert passing_job.id is not None

    result = runner.invoke(app, ["jobs", "list"])

    assert result.exit_code == 0
    assert "failed" in result.output
    assert "ready_to_promote" in result.output
    assert "encoder crashed" in result.output


def test_existing_passing_validation_is_reused_without_running_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _init_config(tmp_path)
    engine = _engine_for_config(config_path)
    now = datetime.now(UTC)

    with Session(engine) as session, session.begin():
        media_file = _store_media_file(session, tmp_path / "movie.mkv", now)
        job = _store_completed_validation_job(
            session,
            tmp_path=tmp_path,
            media_file=media_file,
            now=now,
        )
        output_path = job.output_path
        job_id = job.id
    assert output_path is not None
    assert job_id is not None

    async def fail_if_called(**_kwargs: object) -> object:
        raise AssertionError("completed PASS validation should be reused")

    monkeypatch.setattr("avarch.cli.execute_validation_job", fail_if_called)

    result = runner.invoke(
        app,
        [
            "jobs",
            "validate",
            str(job_id),
        ],
    )

    assert result.exit_code == 0
    assert "Validation PASS" in result.output
    assert "Existing passing validation reused." in result.output


def test_retry_workflow_resets_failed_validate_job_to_validate_stage(
    tmp_path: Path,
) -> None:
    config_path = _init_config(tmp_path)
    app_config = _runtime_config(config_path)
    engine = _engine_for_config(config_path)
    now = datetime.now(UTC)

    with Session(engine) as session, session.begin():
        media_file = _store_media_file(session, tmp_path / "movie.mkv", now)
        probe_result = session.get(ProbeResult, media_file.latest_probe_id)
        assert probe_result is not None
        plan = _localized_plan(tmp_path=tmp_path, media_file=media_file, config=app_config)
        _write_plan(plan)
        plan.output_path.parent.mkdir(parents=True, exist_ok=True)
        plan.output_path.write_bytes(b"encoded")
        session.add(
            Job(
                media_file_id=media_file.id or 0,
                profile_name="av1_1080p_sdr",
                profile_hash=build_profile_hash(
                    ProfileRegistry.from_config(app_config).get("av1_1080p_sdr").profile
                ),
                source_fs_fingerprint=media_file.fs_fingerprint,
                queue_key="failed-validation",
                probe_result_id=probe_result.id,
                probe_hash=probe_result.probe_hash,
                plan_hash=plan.plan_hash,
                plan_path=str(plan.artifacts.plan_json),
                output_path=str(plan.output_path),
                status=JobStatus.FAILED,
                stage=JobStage.VALIDATE,
                last_error_type="ValidationFailed",
                last_error_message="Validation failed required checks: duration",
                created_at=now,
                updated_at=now,
                finished_at=now,
            )
        )

    result = runner.invoke(
        app,
        ["jobs", "retry", "--failed"],
    )

    with Session(engine) as session:
        job = session.get(Job, 1)

    assert result.exit_code == 0
    assert "Failed jobs retry prepared: 1" in result.output
    assert job is not None
    assert job.status == JobStatus.PENDING
    assert job.stage == JobStage.VALIDATE


def test_validation_report_workflow_for_missing_output_fails_required_checks(
    tmp_path: Path,
) -> None:
    now = datetime.now(UTC)
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"media")
    media_file = MediaFile(
        id=1,
        path=str(source.resolve()),
        size_bytes=source.stat().st_size,
        mtime_ns=source.stat().st_mtime_ns,
        device_id=source.stat().st_dev,
        inode=source.stat().st_ino,
        fs_fingerprint=create_file_snapshot(source).fs_fingerprint,
        discovered_at=now,
        last_seen_at=now,
        status=MediaFileStatus.PRESENT,
    )
    plan = _localized_plan(tmp_path=tmp_path, media_file=media_file)
    job = Job(
        id=1,
        media_file_id=1,
        profile_name="av1_1080p_sdr",
        profile_hash="profile",
        source_fs_fingerprint=media_file.fs_fingerprint,
        queue_key="queue",
        plan_hash=plan.plan_hash,
        output_path=str(plan.output_path),
        status=JobStatus.PENDING,
        stage=JobStage.VALIDATE,
        created_at=now,
        updated_at=now,
    )

    report = asyncio.run(
        validate_output(
            job=job,
            plan=plan,
            policy=plan.validation,
            runtime_paths=plan.runtime,
            clock=lambda: now,
        )
    )

    checks = {check.name: check for check in report.checks}
    assert report.passed is False
    assert checks["output_exists"].status == ValidationCheckStatus.FAIL
    assert checks["ffprobe_readable"].status == ValidationCheckStatus.SKIPPED
    assert checks["decode_sample"].status == ValidationCheckStatus.SKIPPED
    assert plan.runtime.validation_report.is_file()


@pytest.mark.skip(reason="requires external NAS or network filesystem metadata behavior")
def test_network_filesystem_metadata_refresh_workflow() -> None:
    # Placeholder for an environment-dependent workflow test.
    pass


@pytest.mark.skip(reason="requires root-level or machine-wide filesystem traversal")
def test_user_points_scanner_at_filesystem_root_workflow() -> None:
    # Placeholder for an environment-dependent workflow test.
    pass


@pytest.mark.skip(reason="requires real memory pressure and would be hostile in CI")
def test_low_memory_oom_encode_workflow() -> None:
    # Placeholder for an environment-dependent workflow test.
    pass


@pytest.mark.skip(reason="requires systemd or a long-running host scheduler")
def test_headless_systemd_scheduler_workflow() -> None:
    # Placeholder for an environment-dependent workflow test.
    pass


def _init_config(tmp_path: Path) -> Path:
    config_path = tmp_path / ".avarch" / "config.toml"
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    return config_path


def _engine_for_config(config_path: Path) -> Engine:
    app_config = load_config(config_path)
    return create_db_engine(resolve_database_url(app_config, config_path))


def _runtime_config(config_path: Path) -> AppConfig:
    app_config = load_config(config_path)
    return app_config.model_copy(
        update={
            "database": app_config.database.model_copy(
                update={"url": resolve_database_url(app_config, config_path)}
            ),
            "app": app_config.app.model_copy(update={"data_dir": config_path.parent / "data"}),
        }
    )


def _tracked_and_probed_movie(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Path]:
    config_path = _init_config(tmp_path)
    media_root = tmp_path / "media"
    media_root.mkdir()
    movie = media_root / "movie.mkv"
    movie.write_bytes(b"media")

    def fake_ffprobe(_path: Path) -> dict[str, object]:
        return sdr_probe_payload()

    monkeypatch.setattr("avarch.cli.run_ffprobe", fake_ffprobe)
    scan = runner.invoke(app, ["scan", str(media_root)])
    probe = runner.invoke(app, ["probe", "--file", str(movie)])
    assert scan.exit_code == 0
    assert probe.exit_code == 0
    return config_path, movie


def _store_media_file(session: Session, path: Path, now: datetime) -> MediaFile:
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
    raw_probe = sdr_probe_payload()
    store_probe_result(
        session,
        media_file=media_file,
        raw_probe=raw_probe,
        normalized_probe=normalize_probe(raw_probe),
        created_at=now,
    )
    session.flush()
    return media_file


def _store_completed_validation_job(
    session: Session,
    *,
    tmp_path: Path,
    media_file: MediaFile,
    now: datetime,
) -> Job:
    plan = _localized_plan(tmp_path=tmp_path, media_file=media_file)
    plan.output_path.parent.mkdir(parents=True, exist_ok=True)
    plan.output_path.write_bytes(b"validated output")
    _write_plan(plan)
    report = _passing_report(plan=plan, media_file=media_file, now=now)
    attempt = JobAttempt(
        job_id=0,
        attempt_number=1,
        stage=JobStage.VALIDATE,
        resource_class=ResourceClass.CHEAP,
        status=AttemptStatus.COMPLETED,
        runner_id="runner",
        started_at=now,
        finished_at=now,
    )
    job = Job(
        media_file_id=media_file.id or 0,
        profile_name="av1_1080p_sdr",
        profile_hash="profile",
        source_fs_fingerprint=media_file.fs_fingerprint,
        queue_key=f"passing-{now.timestamp()}",
        plan_hash=plan.plan_hash,
        plan_path=str(plan.artifacts.plan_json),
        output_path=str(plan.output_path),
        status=JobStatus.VALIDATED,
        stage=JobStage.PROMOTE,
        attempts=1,
        created_at=now,
        updated_at=now,
    )
    session.add(job)
    session.flush()
    attempt.job_id = job.id or 0
    session.add(attempt)
    session.flush()
    result = ValidationResult(
        job_id=job.id or 0,
        attempt_id=attempt.id or 0,
        plan_hash=plan.plan_hash,
        policy_hash=plan.validation.policy_hash,
        output_path=str(plan.output_path),
        output_fs_fingerprint=create_file_snapshot(plan.output_path).fs_fingerprint,
        passed=True,
        details_json=canonical_json(report),
        created_at=now,
    )
    session.add(result)
    session.flush()
    job.latest_validation_id = result.id
    session.add(job)
    return job


def _localized_plan(
    *,
    tmp_path: Path,
    media_file: MediaFile,
    config: AppConfig | None = None,
) -> TranscodePlan:
    base = sample_plan()
    work_dir = tmp_path / ".avarch" / "work" / "workflow"
    artifact_dir = tmp_path / ".avarch" / "plans" / "workflow"
    runtime_dir = work_dir / "runtime"
    script_path = artifact_dir / "movie.vpy"
    output_path = work_dir / "movie.av1.mkv"
    video_output_path = work_dir / "video-only.mkv"
    profile_hash = base.profile_hash
    execution_identity = base.execution_identity
    vapoursynth_identity_hash = base.vapoursynth.identity_hash
    if config is not None:
        profile = ProfileRegistry.from_config(config).get(base.profile_name).profile
        profile_hash = build_profile_hash(profile)
        execution_identity = build_execution_identity()
        vapoursynth_identity_hash = build_vapoursynth_identity_hash(
            generator_version=GENERATOR_VERSION,
            mode=base.vapoursynth.mode,
            output_format=base.vapoursynth.output_format,
            resize_filter=base.vapoursynth.resize_filter,
            template_hash=base.vapoursynth.template_hash,
            script_hash=base.vapoursynth.filter_hash,
            filter_entrypoint=base.vapoursynth.filter_entrypoint,
            filter_api_version=base.vapoursynth.filter_api_version,
        )

    return finalize_plan_hash(
        base.model_copy(
            update={
                "plan_hash": "",
                "input_path": Path(media_file.path),
                "output_path": output_path,
                "temp_dir": work_dir,
                "media_file_id": media_file.id or 1,
                "source_fs_fingerprint": media_file.fs_fingerprint,
                "profile_hash": profile_hash,
                "execution_identity": execution_identity,
                "vapoursynth": base.vapoursynth.model_copy(
                    update={
                        "generator_version": GENERATOR_VERSION,
                        "identity_hash": vapoursynth_identity_hash,
                        "script_path": script_path,
                        "source_path": Path(media_file.path),
                        "index_cache_dir": work_dir / "bestsource",
                    }
                ),
                "av1an": base.av1an.model_copy(
                    update={
                        "input_path": script_path,
                        "video_output_path": video_output_path,
                        "temp_dir": work_dir / "av1an",
                        "working_directory": work_dir,
                    }
                ),
                "mux": base.mux.model_copy(
                    update={
                        "video_input_path": video_output_path,
                        "source_input_path": Path(media_file.path),
                        "output_path": output_path,
                    }
                ),
                "runtime": ExecutionRuntimePaths(
                    runtime_dir=runtime_dir,
                    av1an_stdout_log=runtime_dir / "av1an.stdout.log",
                    av1an_stderr_log=runtime_dir / "av1an.stderr.log",
                    mux_stdout_log=runtime_dir / "mux.stdout.log",
                    mux_stderr_log=runtime_dir / "mux.stderr.log",
                    av1an_stage_marker=runtime_dir / "av1an-stage.json",
                    encode_result=runtime_dir / "encode-result.json",
                    validation_report=runtime_dir / "validation-report.json",
                    validation_decode_stdout_log=runtime_dir / "validation.decode.stdout.log",
                    validation_decode_stderr_log=runtime_dir / "validation.decode.stderr.log",
                ),
                "artifacts": PlanArtifactPaths(
                    artifact_dir=artifact_dir,
                    plan_json=artifact_dir / "plan.json",
                    vapoursynth_script=script_path,
                    av1an_command_json=artifact_dir / "av1an.command.json",
                    validation_policy_json=artifact_dir / "validation-policy.json",
                ),
            }
        )
    )


def _write_plan(plan: TranscodePlan) -> None:
    plan.artifacts.artifact_dir.mkdir(parents=True, exist_ok=True)
    plan.artifacts.plan_json.write_text(canonical_json(plan) + "\n", encoding="utf-8")


def _passing_report(
    *,
    plan: TranscodePlan,
    media_file: MediaFile,
    now: datetime,
) -> ValidationReport:
    return ValidationReport(
        plan_hash=plan.plan_hash,
        policy_hash=plan.validation.policy_hash,
        source_path=Path(media_file.path),
        output_path=plan.output_path,
        source_fs_fingerprint_before=media_file.fs_fingerprint,
        source_fs_fingerprint_after=media_file.fs_fingerprint,
        output_fs_fingerprint_before=create_file_snapshot(plan.output_path).fs_fingerprint,
        output_fs_fingerprint_after=create_file_snapshot(plan.output_path).fs_fingerprint,
        passed=True,
        checks=[
            ValidationCheck(
                name="output_exists",
                status=ValidationCheckStatus.PASS,
                required=True,
                expected=True,
                observed=True,
            )
        ],
        warnings=[],
        observed=ObservedValidationMedia(
            output_size_bytes=plan.output_path.stat().st_size,
            container="matroska,webm",
            duration_seconds=plan.validation.source_duration_seconds,
            video_streams=[],
            audio_streams=[],
            subtitle_streams=[],
        ),
        started_at=now,
        finished_at=now,
    )
