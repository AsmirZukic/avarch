from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from avarch.cli import app
from avarch.scheduler import SchedulerRunSummary

runner = CliRunner()


def test_enqueue_command_creates_jobs(tmp_path: Path) -> None:
    config_path = _init_config(tmp_path)
    media_root = tmp_path / "media"
    media_root.mkdir()
    movie = media_root / "movie.mkv"
    movie.write_bytes(b"media")
    runner.invoke(app, ["scan", str(media_root), "--config", str(config_path)])

    result = runner.invoke(
        app,
        ["enqueue", "--profile", "av1_1080p_sdr", "--config", str(config_path)],
    )

    assert result.exit_code == 0
    assert "Created jobs:    1" in result.output


def test_jobs_command_lists_queued_jobs(tmp_path: Path) -> None:
    config_path = _init_config(tmp_path)
    media_root = tmp_path / "media"
    media_root.mkdir()
    movie = media_root / "movie.mkv"
    movie.write_bytes(b"media")
    runner.invoke(app, ["scan", str(media_root), "--config", str(config_path)])
    runner.invoke(app, ["enqueue", "--profile", "av1_1080p_sdr", "--config", str(config_path)])

    result = runner.invoke(app, ["jobs", "list", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "pending" in result.output
    assert "movie.mkv" in result.output


def test_pause_command_sets_persistent_state(tmp_path: Path) -> None:
    config_path = _init_config(tmp_path)

    result = runner.invoke(app, ["scheduler", "pause", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "Scheduler pause requested" in result.output


def test_queue_retry_preview_runs_without_confirm(tmp_path: Path) -> None:
    config_path = _init_config(tmp_path)

    result = runner.invoke(app, ["queue", "retry", "--config", str(config_path)])

    assert result.exit_code == 0
    assert "Queue retry preview" in result.output


def test_run_command_invokes_scheduler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _init_config(tmp_path)
    calls: list[str] = []

    async def fake_run_scheduler(**_kwargs: object) -> SchedulerRunSummary:
        calls.append("called")
        return SchedulerRunSummary(completed=0, failed=0, skipped=0, idle=True)

    monkeypatch.setattr("avarch.cli.run_scheduler", fake_run_scheduler)

    result = runner.invoke(app, ["scheduler", "run", "--config", str(config_path)])

    assert result.exit_code == 0
    assert calls == ["called"]
    assert "Scheduler started" in result.output


def _init_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "avarch.toml"
    result = runner.invoke(app, ["init", "--config", str(config_path)])
    assert result.exit_code == 0
    return config_path
