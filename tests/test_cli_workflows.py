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


def test_first_time_setup_workflow_checks_help_init_config_and_doctor(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / ".avarch" / "config.toml"
    profiles_path = tmp_path / ".avarch" / "profiles"

    help_result = runner.invoke(app, ["--help"])
    init_result = runner.invoke(app, ["init"])
    doctor_result = runner.invoke(app, ["doctor"])

    assert help_result.exit_code == 0
    assert init_result.exit_code == 0
    assert "[profile_registry]" in config_path.read_text(encoding="utf-8")
    assert profiles_path.is_dir()
    assert f"Profiles dir: {profiles_path}" in init_result.output
    assert "Profiles: av1_1080p_sdr" in init_result.output
    resolved_profile = ProfileRegistry.from_config(load_config(config_path)).get("av1_1080p_sdr")
    assert resolved_profile.origin == ProfileOrigin.BUILTIN
    assert doctor_result.exit_code == 0
    assert "PASS config_exists" in doctor_result.output


def test_first_time_setup_workflow_reports_broken_config(tmp_path: Path) -> None:
    config_path = tmp_path / ".avarch" / "config.toml"
    (tmp_path / ".avarch").mkdir()
    config_path.write_text("[broken\n", encoding="utf-8")

    result = runner.invoke(app, ["doctor"])

    assert result.exit_code != 0
    assert "FAIL config_parses" in result.output


def test_inventory_workflow_scans_lists_changed_and_missing_files(tmp_path: Path) -> None:
    _init_config(tmp_path)
    media_root = tmp_path / "media"
    media_root.mkdir()
    movie = media_root / "movie.mkv"
    movie.write_bytes(b"first")

    scan_result = runner.invoke(app, ["scan", str(media_root)])
    files_result = runner.invoke(app, ["files", "list"])

    movie.write_bytes(b"changed bytes")
    changed_scan_result = runner.invoke(
        app,
        ["scan", str(media_root)],
    )
    changed_files_result = runner.invoke(
        app,
        ["files", "list", "--changed"],
    )

    movie.unlink()
    missing_scan_result = runner.invoke(
        app,
        ["scan", str(media_root)],
    )
    missing_files_result = runner.invoke(
        app,
        ["files", "list", "--changed"],
    )

    assert scan_result.exit_code == 0
    assert "Added:     1" in scan_result.output
    assert files_result.exit_code == 0
    assert "media/movie.mkv" in files_result.output
    assert changed_scan_result.exit_code == 0
    assert "Changed:   1" in changed_scan_result.output
    assert "changed" in changed_files_result.output
    assert missing_scan_result.exit_code == 0
    assert "Missing:   1" in missing_scan_result.output
    assert "missing" in missing_files_result.output


def test_inventory_workflow_rejects_invalid_scan_root(tmp_path: Path) -> None:
    _init_config(tmp_path)

    result = runner.invoke(app, ["scan", str(tmp_path / "missing")])

    assert result.exit_code != 0
    assert "Root does not exist" in result.output


def test_probe_inspect_and_plan_workflow_builds_reviewable_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_config(tmp_path)
    media_root = tmp_path / "media"
    media_root.mkdir()
    movie = media_root / "movie.mkv"
    movie.write_bytes(b"media")
    monkeypatch.setattr("avarch.bootstrap.run_ffprobe", _fake_ffprobe)

    scan_result = runner.invoke(app, ["scan", str(media_root)])
    probe_result = runner.invoke(app, ["probe", "--file", str(movie)])
    inspect_result = runner.invoke(app, ["files", "show", "--file", str(movie)])
    plan_result = runner.invoke(
        app,
        ["plan", "--profile", "av1_1080p_sdr", "--file", str(movie)],
    )
    repeated_plan_result = runner.invoke(
        app,
        ["plan", "--profile", "av1_1080p_sdr", "--file", str(movie)],
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
    assert "plan-current" in repeated_plan_result.output


def test_probe_inspect_and_plan_workflow_reports_user_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_config(tmp_path)
    media_root = tmp_path / "media"
    media_root.mkdir()
    movie = media_root / "movie.mkv"
    movie.write_bytes(b"media")
    monkeypatch.setattr("avarch.bootstrap.run_ffprobe", _fake_ffprobe)

    scan_result = runner.invoke(app, ["scan", str(media_root)])
    plan_before_probe = runner.invoke(
        app,
        ["plan", "--profile", "av1_1080p_sdr", "--file", str(movie)],
    )
    unknown_profile = runner.invoke(
        app,
        ["plan", "--profile", "missing", "--file", str(movie)],
    )
    probe_result = runner.invoke(app, ["probe", "--file", str(movie)])
    movie.write_bytes(b"changed after probe")
    stale_scan_result = runner.invoke(
        app,
        ["scan", str(media_root)],
    )
    stale_plan = runner.invoke(
        app,
        ["plan", "--profile", "av1_1080p_sdr", "--file", str(movie)],
    )

    assert scan_result.exit_code == 0
    assert plan_before_probe.exit_code == 0
    assert "probe-missing" in plan_before_probe.output
    assert unknown_profile.exit_code != 0
    assert "Unknown profile: missing" in unknown_profile.output
    assert probe_result.exit_code == 0
    assert stale_scan_result.exit_code == 0
    assert stale_plan.exit_code == 0
    assert "probe-stale" in stale_plan.output


def _fake_ffprobe(_path: Path) -> dict[str, Any]:
    return sdr_probe_payload()


def _init_config(tmp_path: Path) -> Path:
    config_path = tmp_path / ".avarch" / "config.toml"
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    return config_path


def _artifact_dir_from_output(output: str) -> Path:
    for line in output.splitlines():
        if line.startswith("Artifact dir: "):
            return Path(line.removeprefix("Artifact dir: "))
    raise AssertionError("plan output did not contain an artifact directory")
