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


def test_workflow_run_orchestrates_full_pipeline_with_promotion_mode(
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
    ) -> None:
        calls.append(("run", resume, detached, managed_child, mode))

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
                status=JobStatus.READY_TO_PROMOTE,
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
        ("run", True, False, False, "foreground"),
        ("verify", ["plan-hash"]),
        ("promote", 42, PromotionMode.REPLACE_ATOMIC, False, True, False),
    ]
    assert "== scan ==" in result.output
    assert "== verify ==" in result.output
    assert "Validated jobs ready for promotion: 1" in result.output


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

    def fake_run_queue(**_kwargs: object) -> None:
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
    assert calls == [("promote", True, False)]
