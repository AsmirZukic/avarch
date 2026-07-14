from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from avarch.adapters.sqlite.models import Job
from avarch.application.enqueue import PlanEnqueueSummary
from avarch.cli import app
from avarch.domain.jobs import JobStage, JobStatus
from avarch.models.promotion import PromotionMode

runner = CliRunner()


def test_workflow_run_promotes_replace_atomic_during_scheduler_when_confirmed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0

    media_root = tmp_path / "media"
    media_root.mkdir()
    movie = media_root / "movie.mkv"
    movie.write_bytes(b"media")
    calls: list[tuple[Any, ...]] = []

    def fake_scan(*, roots: list[Path] | None = None) -> None:
        calls.append(("scan", roots))

    def fake_probe_file(*, files: list[Path] | None = None, force: bool = False) -> None:
        calls.append(("probe", files, force))

    def fake_plan_file(
        *,
        profile: str,
        files: list[Path] | None = None,
        force: bool = False,
        check_vpy: bool = False,
    ) -> None:
        calls.append(("plan", profile, files, force, check_vpy))

    def fake_enqueue_selected_plans(**kwargs: object) -> PlanEnqueueSummary:
        calls.append(
            (
                "enqueue",
                kwargs["file_selectors"],
                kwargs["plan_selectors"],
                kwargs["priority"],
            )
        )
        return PlanEnqueueSummary(
            selected=1,
            created=1,
            skipped=0,
            already_queued=0,
            already_done=0,
            stale=0,
            plan_hashes=("plan-hash",),
        )

    def fake_run_queue(
        *,
        resume: bool = False,
        detached: bool = False,
        managed_child: bool = False,
        mode: str = "foreground",
        promote: bool = True,
    ) -> None:
        calls.append(("run", resume, detached, managed_child, mode, promote))

    def fake_workflow_jobs_for_plan_hashes(
        _database_url: str,
        plan_hashes: list[str],
    ) -> list[Job]:
        calls.append(("verify", plan_hashes))
        now = datetime.now(UTC)
        return [
            Job(
                id=42,
                media_file_id=1,
                profile_name="av1_1080p_sdr",
                profile_hash="profile",
                source_fs_fingerprint="fingerprint",
                queue_key="queue",
                plan_hash="plan-hash",
                output_path=str(tmp_path / "movie.av1.mkv"),
                status=JobStatus.PROMOTED,
                stage=JobStage.PROMOTE,
                created_at=now,
                updated_at=now,
            )
        ]

    def fake_promote_job(
        *,
        job_id: int,
        mode: PromotionMode,
        dry_run: bool,
        confirm: bool,
        recover: bool,
    ) -> None:
        calls.append(("promote", job_id, mode, dry_run, confirm, recover))

    monkeypatch.setattr("avarch.cli.scan", fake_scan)
    monkeypatch.setattr("avarch.cli.probe_file", fake_probe_file)
    monkeypatch.setattr("avarch.cli.plan_file", fake_plan_file)
    monkeypatch.setattr("avarch.cli._enqueue_selected_plans", fake_enqueue_selected_plans)
    monkeypatch.setattr("avarch.cli.run_queue", fake_run_queue)
    monkeypatch.setattr(
        "avarch.cli._workflow_jobs_for_plan_hashes",
        fake_workflow_jobs_for_plan_hashes,
    )
    monkeypatch.setattr("avarch.cli.promote_job", fake_promote_job)

    result = runner.invoke(
        app,
        [
            "workflow",
            "run",
            "--profile",
            "av1_1080p_sdr",
            "--file",
            str(movie),
            "--priority",
            "7",
            "--mode",
            "replace-atomic",
            "--confirm",
            str(media_root),
        ],
    )

    assert result.exit_code == 0
    assert calls == [
        ("scan", [media_root]),
        ("probe", [movie], False),
        ("plan", "av1_1080p_sdr", [movie], False, False),
        ("enqueue", [movie], None, 7),
        ("run", True, False, False, "foreground", True),
        ("verify", ["plan-hash"]),
    ]
    assert "== scan ==" in result.output
    assert "== verify ==" in result.output
    assert "Promotion completed by scheduler." in result.output


def test_workflow_run_previews_promotion_without_confirm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0

    now = datetime.now(UTC)
    calls: list[tuple[str, bool, bool]] = []

    def fake_scan(*, roots: list[Path] | None = None) -> None:
        del roots

    def fake_probe_file(*, files: list[Path] | None = None, force: bool = False) -> None:
        del files, force

    def fake_plan_file(
        *,
        profile: str,
        files: list[Path] | None = None,
        force: bool = False,
        check_vpy: bool = False,
    ) -> None:
        del profile, files, force, check_vpy

    def fake_enqueue_selected_plans(**_kwargs: object) -> PlanEnqueueSummary:
        return PlanEnqueueSummary(
            selected=1,
            created=1,
            skipped=0,
            already_queued=0,
            already_done=0,
            stale=0,
            plan_hashes=("plan-hash",),
        )

    def fake_run_queue(**kwargs: object) -> None:
        calls.append(("run", bool(kwargs["promote"]), False))
        return None

    def fake_workflow_jobs_for_plan_hashes(
        _database_url: str,
        _plan_hashes: list[str],
    ) -> list[Job]:
        return [
            Job(
                id=7,
                media_file_id=1,
                profile_name="av1_1080p_sdr",
                profile_hash="profile",
                source_fs_fingerprint="fingerprint",
                queue_key="queue",
                plan_hash="plan-hash",
                status=JobStatus.READY_TO_PROMOTE,
                stage=JobStage.PROMOTE,
                created_at=now,
                updated_at=now,
            )
        ]

    def fake_promote_job(**kwargs: object) -> None:
        calls.append(("promote", bool(kwargs["dry_run"]), bool(kwargs["confirm"])))

    monkeypatch.setattr("avarch.cli.scan", fake_scan)
    monkeypatch.setattr("avarch.cli.probe_file", fake_probe_file)
    monkeypatch.setattr("avarch.cli.plan_file", fake_plan_file)
    monkeypatch.setattr("avarch.cli._enqueue_selected_plans", fake_enqueue_selected_plans)
    monkeypatch.setattr("avarch.cli.run_queue", fake_run_queue)
    monkeypatch.setattr(
        "avarch.cli._workflow_jobs_for_plan_hashes",
        fake_workflow_jobs_for_plan_hashes,
    )
    monkeypatch.setattr("avarch.cli.promote_job", fake_promote_job)

    result = runner.invoke(app, ["workflow", "run", "--profile", "av1_1080p_sdr"])

    assert result.exit_code == 0
    assert calls == [("run", False, False), ("promote", True, False)]


def test_workflow_run_processes_mixed_terminal_and_promotable_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    init_result = runner.invoke(app, ["init"])
    assert init_result.exit_code == 0

    now = datetime.now(UTC)
    calls: list[tuple[Any, ...]] = []

    def fake_scan(*, roots: list[Path] | None = None) -> None:
        del roots

    def fake_probe_file(*, files: list[Path] | None = None, force: bool = False) -> None:
        del files, force

    def fake_plan_file(
        *,
        profile: str,
        files: list[Path] | None = None,
        force: bool = False,
        check_vpy: bool = False,
    ) -> None:
        del profile, files, force, check_vpy

    def fake_enqueue_selected_plans(**_kwargs: object) -> PlanEnqueueSummary:
        return PlanEnqueueSummary(
            selected=5,
            created=1,
            skipped=4,
            already_queued=0,
            already_done=4,
            stale=0,
            plan_hashes=("skip-av1", "encode", "size", "existing-promote", "skip-match"),
        )

    def fake_run_queue(**kwargs: object) -> None:
        calls.append(("run", kwargs["promote"]))

    def fake_workflow_jobs_for_plan_hashes(
        _database_url: str,
        plan_hashes: list[str],
    ) -> list[Job]:
        calls.append(("verify", tuple(plan_hashes)))
        return [
            _job_item(1, "skip-av1", JobStatus.SKIPPED, JobStage.PLAN, now),
            _job_item(2, "encode", JobStatus.READY_TO_PROMOTE, JobStage.PROMOTE, now),
            _job_item(3, "size", JobStatus.SIZE_REJECTED, JobStage.CLEANUP, now),
            _job_item(
                4,
                "existing-promote",
                JobStatus.READY_TO_PROMOTE,
                JobStage.PROMOTE,
                now,
            ),
            _job_item(5, "skip-match", JobStatus.SKIPPED, JobStage.PLAN, now),
        ]

    def fake_promote_job(**kwargs: object) -> None:
        calls.append(
            (
                "promote",
                kwargs["job_id"],
                kwargs["mode"],
                kwargs["dry_run"],
                kwargs["confirm"],
            )
        )

    monkeypatch.setattr("avarch.cli.scan", fake_scan)
    monkeypatch.setattr("avarch.cli.probe_file", fake_probe_file)
    monkeypatch.setattr("avarch.cli.plan_file", fake_plan_file)
    monkeypatch.setattr("avarch.cli._enqueue_selected_plans", fake_enqueue_selected_plans)
    monkeypatch.setattr("avarch.cli.run_queue", fake_run_queue)
    monkeypatch.setattr(
        "avarch.cli._workflow_jobs_for_plan_hashes",
        fake_workflow_jobs_for_plan_hashes,
    )
    monkeypatch.setattr("avarch.cli.promote_job", fake_promote_job)

    result = runner.invoke(
        app,
        [
            "workflow",
            "run",
            "--profile",
            "av1_1080p_sdr",
            "--mode",
            "move-original-to-backup",
            "--confirm",
        ],
    )

    assert result.exit_code == 0
    assert calls == [
        ("run", False),
        ("verify", ("skip-av1", "encode", "size", "existing-promote", "skip-match")),
        ("promote", 2, PromotionMode.MOVE_ORIGINAL_TO_BACKUP, False, True),
        ("promote", 4, PromotionMode.MOVE_ORIGINAL_TO_BACKUP, False, True),
    ]
    assert "Validated jobs ready for promotion: 2" in result.output


def _job_item(
    job_id: int,
    plan_hash: str,
    status: JobStatus,
    stage: JobStage,
    now: datetime,
) -> Job:
    return Job(
        id=job_id,
        media_file_id=job_id,
        profile_name="av1_1080p_sdr",
        profile_hash="profile",
        source_fs_fingerprint="fingerprint",
        queue_key=plan_hash,
        plan_hash=plan_hash,
        status=status,
        stage=stage,
        created_at=now,
        updated_at=now,
    )
