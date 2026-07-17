from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from avarch.cli import app

runner = CliRunner()


def test_scheduler_watch_once_renders_empty_stopped_scheduler(tmp_path: Path) -> None:
    _init_config(tmp_path)

    result = runner.invoke(app, ["scheduler", "watch", "--once"])

    assert result.exit_code == 0
    assert "Avarch Scheduler" in result.output
    assert "Scheduler" in result.output
    assert "stopped" in result.output
    assert "No active jobs" in result.output or "Active none" in result.output


def test_scheduler_watch_once_no_color_has_no_ansi(tmp_path: Path) -> None:
    _init_config(tmp_path)

    result = runner.invoke(app, ["scheduler", "watch", "--once", "--no-color"])

    assert result.exit_code == 0
    assert "\x1b" not in result.output


def test_scheduler_watch_json_prints_raw_snapshot(tmp_path: Path) -> None:
    _init_config(tmp_path)

    result = runner.invoke(app, ["scheduler", "watch", "--json"])

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["scheduler"]["state"] == "stopped"
    assert payload["pipeline"]["queued"] == 0
    assert payload["active_jobs"] == []
    assert payload["resources"] is None
    assert "Panel" not in result.output
    assert "\x1b" not in result.output


def test_scheduler_watch_live_rejects_non_tty_with_guidance(tmp_path: Path) -> None:
    _init_config(tmp_path)

    result = runner.invoke(app, ["scheduler", "watch"])

    assert result.exit_code == 1
    assert "Use --once or --json" in result.output


def test_scheduler_watch_missing_workspace_uses_normal_workspace_error(tmp_path: Path) -> None:
    result = runner.invoke(app, ["scheduler", "watch", "--once"])

    assert result.exit_code != 0
    assert "No Avarch workspace found" in result.output


def _init_config(tmp_path: Path) -> Path:
    config_path = tmp_path / ".avarch" / "config.toml"
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    return config_path
