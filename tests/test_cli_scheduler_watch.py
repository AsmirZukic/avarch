from __future__ import annotations

import asyncio
import json
from pathlib import Path

from typer.testing import CliRunner

from avarch import cli as cli_module
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


def test_scheduler_watch_help_documents_modes_and_options() -> None:
    result = runner.invoke(app, ["scheduler", "watch", "--help"])

    assert result.exit_code == 0
    assert "--once" in result.output
    assert "--json" in result.output
    assert "--interval" in result.output
    assert "--no-color" in result.output


def test_readme_documents_scheduler_watch_workflow() -> None:
    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
    readme_text = " ".join(readme.split())

    assert "avarch scheduler watch --once" in readme
    assert "avarch scheduler watch --json" in readme
    assert "Ctrl+C detaches from watch mode" in readme_text
    assert "Direct Docker usage must pass `-it`" in readme_text
    assert "Linux/POSIX first" in readme_text
    assert "interactive shortcuts are unavailable" in readme_text


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


def test_scheduler_watch_live_uses_alternate_screen(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    seen: dict[str, object] = {}

    class FakeLive:
        def __init__(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            seen["screen"] = kwargs.get("screen")

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *args) -> None:  # type: ignore[no-untyped-def]
            return None

    class FakeLoop:
        def __init__(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
            seen["resource_sampler"] = kwargs["resource_sampler"]

        async def run(self) -> None:
            seen["ran"] = True

    monkeypatch.setattr(cli_module, "Live", FakeLive)
    monkeypatch.setattr(cli_module, "_LiveSchedulerSnapshotQuery", lambda **kwargs: object())
    monkeypatch.setattr(cli_module, "SchedulerWatchLoop", FakeLoop)
    monkeypatch.setattr(cli_module, "scheduler_resource_sampler", lambda **kwargs: "sampler")

    asyncio.run(
        cli_module._run_scheduler_watch_live(
            database_url="sqlite:///watch.db",
            workspace_root=tmp_path,
            interval_seconds=0.1,
            no_color=True,
        )
    )

    assert seen["screen"] is True
    assert seen["resource_sampler"] == "sampler"
    assert seen["ran"] is True


def test_scheduler_watch_missing_workspace_uses_normal_workspace_error(tmp_path: Path) -> None:
    result = runner.invoke(app, ["scheduler", "watch", "--once"])

    assert result.exit_code != 0
    assert "No Avarch workspace found" in result.output


def _init_config(tmp_path: Path) -> Path:
    config_path = tmp_path / ".avarch" / "config.toml"
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    return config_path
