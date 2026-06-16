from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from avarch.cli import app
from avarch.config import load_config
from avarch.profiles.registry import ProfileOrigin, ProfileRegistry
from tests.probe_fixtures import sdr_probe_payload

runner = CliRunner()


def test_first_time_setup_workflow_checks_help_init_config_doctor_and_db(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "avarch.toml"
    profiles_path = tmp_path / "profiles"
    profile_path = profiles_path / "av1_1080p_sdr.toml"
    readme_path = profiles_path / "README.md"

    help_result = runner.invoke(app, ["--help"])
    init_result = runner.invoke(app, ["init", "--config", str(config_path)])
    doctor_result = runner.invoke(app, ["doctor", "--config", str(config_path)])
    db_current_result = runner.invoke(app, ["db", "current", "--config", str(config_path)])
    db_upgrade_result = runner.invoke(app, ["db", "upgrade", "--config", str(config_path)])

    assert help_result.exit_code == 0
    assert init_result.exit_code == 0
    assert "[profile_registry]" in config_path.read_text(encoding="utf-8")
    assert profiles_path.is_dir()
    assert profile_path.is_file()
    assert readme_path.is_file()
    assert "Inspect the .toml files here" in readme_path.read_text(encoding="utf-8")
    assert f"Profiles dir: {profiles_path}" in init_result.output
    assert "Profiles: av1_1080p_sdr" in init_result.output
    resolved_profile = ProfileRegistry.from_config(load_config(config_path)).get("av1_1080p_sdr")
    assert resolved_profile.origin == ProfileOrigin.USER
    assert doctor_result.exit_code == 0
    assert "PASS config_exists" in doctor_result.output
    assert db_current_result.exit_code == 0
    assert db_current_result.output.strip()
    assert db_upgrade_result.exit_code == 0
    assert "Database upgraded" in db_upgrade_result.output


def test_first_time_setup_workflow_reports_broken_config(tmp_path: Path) -> None:
    config_path = tmp_path / "avarch.toml"
    config_path.write_text("[broken\n", encoding="utf-8")

    result = runner.invoke(app, ["doctor", "--config", str(config_path)])

    assert result.exit_code != 0
    assert "FAIL config_parses" in result.output


def test_inventory_workflow_scans_lists_changed_and_missing_files(tmp_path: Path) -> None:
    config_path = _init_config(tmp_path)
    media_root = tmp_path / "media"
    media_root.mkdir()
    movie = media_root / "movie.mkv"
    movie.write_bytes(b"first")

    scan_result = runner.invoke(app, ["scan", str(media_root), "--config", str(config_path)])
    files_result = runner.invoke(app, ["files", "--config", str(config_path)])

    movie.write_bytes(b"changed bytes")
    changed_scan_result = runner.invoke(
        app,
        ["scan", str(media_root), "--config", str(config_path)],
    )
    changed_files_result = runner.invoke(
        app,
        ["files", "--changed", "--config", str(config_path)],
    )

    movie.unlink()
    missing_scan_result = runner.invoke(
        app,
        ["scan", str(media_root), "--config", str(config_path)],
    )
    missing_files_result = runner.invoke(
        app,
        ["files", "--changed", "--config", str(config_path)],
    )

    assert scan_result.exit_code == 0
    assert "Added:     1" in scan_result.output
    assert files_result.exit_code == 0
    assert str(movie.resolve()) in files_result.output
    assert changed_scan_result.exit_code == 0
    assert "Changed:   1" in changed_scan_result.output
    assert "changed" in changed_files_result.output
    assert missing_scan_result.exit_code == 0
    assert "Missing:   1" in missing_scan_result.output
    assert "missing" in missing_files_result.output


def test_inventory_workflow_rejects_invalid_scan_root(tmp_path: Path) -> None:
    config_path = _init_config(tmp_path)

    result = runner.invoke(app, ["scan", str(tmp_path / "missing"), "--config", str(config_path)])

    assert result.exit_code != 0
    assert "Root does not exist" in result.output


def test_probe_inspect_and_plan_workflow_builds_reviewable_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _init_config(tmp_path)
    media_root = tmp_path / "media"
    media_root.mkdir()
    movie = media_root / "movie.mkv"
    movie.write_bytes(b"media")
    monkeypatch.setattr("avarch.cli.run_ffprobe", _fake_ffprobe)

    scan_result = runner.invoke(app, ["scan", str(media_root), "--config", str(config_path)])
    probe_result = runner.invoke(app, ["probe", str(movie), "--config", str(config_path)])
    inspect_result = runner.invoke(app, ["inspect", str(movie), "--config", str(config_path)])
    plan_result = runner.invoke(
        app,
        ["plan", str(movie), "--profile", "av1_1080p_sdr", "--config", str(config_path)],
    )
    repeated_plan_result = runner.invoke(
        app,
        ["plan", str(movie), "--profile", "av1_1080p_sdr", "--config", str(config_path)],
    )

    artifact_dir = _artifact_dir_from_output(plan_result.output)
    assert scan_result.exit_code == 0
    assert probe_result.exit_code == 0
    assert "Probe hash:" in probe_result.output
    assert inspect_result.exit_code == 0
    assert "Container: matroska,webm" in inspect_result.output
    assert plan_result.exit_code == 0
    assert "Plan hash:" in plan_result.output
    assert (artifact_dir / "plan.json").is_file()
    assert (artifact_dir / "av1an.command.json").is_file()
    assert (artifact_dir / "validation-policy.json").is_file()
    assert (artifact_dir / "movie.vpy").is_file()
    assert repeated_plan_result.exit_code == 0
    assert _artifact_dir_from_output(repeated_plan_result.output) == artifact_dir


def test_probe_inspect_and_plan_workflow_reports_user_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _init_config(tmp_path)
    media_root = tmp_path / "media"
    media_root.mkdir()
    movie = media_root / "movie.mkv"
    movie.write_bytes(b"media")
    monkeypatch.setattr("avarch.cli.run_ffprobe", _fake_ffprobe)

    scan_result = runner.invoke(app, ["scan", str(media_root), "--config", str(config_path)])
    plan_before_probe = runner.invoke(
        app,
        ["plan", str(movie), "--profile", "av1_1080p_sdr", "--config", str(config_path)],
    )
    unknown_profile = runner.invoke(
        app,
        ["plan", str(movie), "--profile", "missing", "--config", str(config_path)],
    )
    probe_result = runner.invoke(app, ["probe", str(movie), "--config", str(config_path)])
    movie.write_bytes(b"changed after probe")
    stale_scan_result = runner.invoke(
        app,
        ["scan", str(media_root), "--config", str(config_path)],
    )
    stale_plan = runner.invoke(
        app,
        ["plan", str(movie), "--profile", "av1_1080p_sdr", "--config", str(config_path)],
    )

    assert scan_result.exit_code == 0
    assert plan_before_probe.exit_code != 0
    assert "No canonical probe result exists" in plan_before_probe.output
    assert unknown_profile.exit_code != 0
    assert "Unknown profile: missing" in unknown_profile.output
    assert probe_result.exit_code == 0
    assert stale_scan_result.exit_code == 0
    assert stale_plan.exit_code != 0
    assert "Run avarch probe for this file again." in stale_plan.output


def test_tui_workflow_can_be_opened_before_and_after_init(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class FakeTuiApp:
        def __init__(
            self,
            *,
            initialized: bool,
            database_url: str,
            exit_after_mount: bool,
        ) -> None:
            calls.append(
                {
                    "initialized": initialized,
                    "database_url": database_url,
                    "exit_after_mount": exit_after_mount,
                }
            )

        def run(self, *, headless: bool) -> None:
            calls[-1]["headless"] = headless

    monkeypatch.setattr("avarch.cli.AvarchTuiApp", FakeTuiApp)
    config_path = tmp_path / "avarch.toml"

    before_init = runner.invoke(app, ["tui", "--config", str(config_path)])
    init_result = runner.invoke(app, ["init", "--config", str(config_path), "--force"])
    after_init = runner.invoke(app, ["tui", "--config", str(config_path)])

    assert before_init.exit_code == 0
    assert init_result.exit_code == 0
    assert after_init.exit_code == 0
    assert len(calls) == 2
    assert all(call["initialized"] is True for call in calls)
    assert all(call["exit_after_mount"] is True for call in calls)
    assert all(call["headless"] is True for call in calls)


def _fake_ffprobe(_path: Path) -> dict[str, Any]:
    return sdr_probe_payload()


def _init_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "avarch.toml"
    result = runner.invoke(app, ["init", "--config", str(config_path)])
    assert result.exit_code == 0
    return config_path


def _artifact_dir_from_output(output: str) -> Path:
    for line in output.splitlines():
        if line.startswith("Artifact dir: "):
            return Path(line.removeprefix("Artifact dir: "))
    raise AssertionError("plan output did not contain an artifact directory")
