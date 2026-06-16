from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from avarch.cli import app
from avarch.vapoursynth import VspipeProcessError
from tests.probe_fixtures import representative_probe_payload, sdr_probe_payload

runner = CliRunner()


def test_configured_scan_workflow_handles_roots_extensions_exclusions_and_symlinks(
    tmp_path: Path,
) -> None:
    media_root = tmp_path / "media"
    media_root.mkdir()
    movie = media_root / "movie.MKV"
    ignored_text = media_root / "notes.txt"
    excluded_dir = media_root / ".avarch-work"
    excluded_movie = excluded_dir / "excluded.mkv"
    nested = media_root / "nested"
    nested_movie = nested / "episode.mp4"
    movie.write_bytes(b"movie")
    ignored_text.write_text("not media", encoding="utf-8")
    excluded_dir.mkdir()
    excluded_movie.write_bytes(b"excluded")
    nested.mkdir()
    nested_movie.write_bytes(b"nested")
    (media_root / "linked.mkv").symlink_to(movie)

    config_path = _write_inventory_config(tmp_path, roots=[media_root])

    scan_result = runner.invoke(app, ["scan", "--config", str(config_path)])
    files_result = runner.invoke(app, ["files", "--config", str(config_path)])

    assert scan_result.exit_code == 0
    assert "Added:     2" in scan_result.output
    assert str(movie.resolve()) in files_result.output
    assert str(nested_movie.resolve()) in files_result.output
    assert str(ignored_text.resolve()) not in files_result.output
    assert str(excluded_movie.resolve()) not in files_result.output
    assert "linked.mkv" not in files_result.output


def test_empty_extension_workflow_can_mark_previously_tracked_files_missing(
    tmp_path: Path,
) -> None:
    config_path = _init_config(tmp_path)
    media_root = tmp_path / "media"
    media_root.mkdir()
    movie = media_root / "movie.mkv"
    movie.write_bytes(b"media")

    first_scan = runner.invoke(app, ["scan", str(media_root), "--config", str(config_path)])
    _replace_config_line(
        config_path,
        'extensions = [".mkv", ".mp4", ".m4v", ".mov", ".avi", ".webm", ".ts", ".m2ts"]',
        "extensions = []",
    )
    second_scan = runner.invoke(app, ["scan", str(media_root), "--config", str(config_path)])
    changed_files = runner.invoke(app, ["files", "--changed", "--config", str(config_path)])

    assert first_scan.exit_code == 0
    assert second_scan.exit_code == 0
    assert "Missing:   1" in second_scan.output
    assert "missing" in changed_files.output
    assert str(movie.resolve()) in changed_files.output


def test_media_catalog_workflow_can_stop_after_probe_and_inspect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, movie = _tracked_movie(tmp_path)
    monkeypatch.setattr("avarch.cli.run_ffprobe", _fake_sdr_ffprobe)

    probe_result = runner.invoke(app, ["probe", str(movie), "--config", str(config_path)])
    inspect_result = runner.invoke(app, ["inspect", str(movie), "--config", str(config_path)])

    assert probe_result.exit_code == 0
    assert inspect_result.exit_code == 0
    assert "Probe hash:" in inspect_result.output
    assert not (tmp_path / ".avarch" / "plans").exists()


def test_plan_workflow_materializes_bundle_for_manual_av1an_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, movie = _tracked_movie(tmp_path)
    monkeypatch.setattr("avarch.cli.run_ffprobe", _fake_sdr_ffprobe)
    _probe(config_path, movie)

    plan_result = runner.invoke(
        app,
        ["plan", str(movie), "--profile", "av1_1080p_sdr", "--config", str(config_path)],
    )

    artifact_dir = _artifact_dir_from_output(plan_result.output)
    plan = json.loads((artifact_dir / "plan.json").read_text(encoding="utf-8"))
    command = json.loads((artifact_dir / "av1an.command.json").read_text(encoding="utf-8"))
    script_path = artifact_dir / "movie.vpy"

    assert plan_result.exit_code == 0
    assert command["input_path"] == str(script_path)
    assert plan["vapoursynth"]["script_path"] == str(script_path)
    assert not Path(command["video_output_path"]).exists()
    assert "Dry run only. No encoding was started." in plan_result.output


def test_plan_check_vpy_workflow_runs_runtime_validation_when_requested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, movie = _tracked_movie(tmp_path)
    monkeypatch.setattr("avarch.cli.run_ffprobe", _fake_sdr_ffprobe)
    _probe(config_path, movie)
    calls: list[Path] = []

    def fake_check(script_path: Path) -> object:
        calls.append(script_path)
        return object()

    monkeypatch.setattr("avarch.cli.check_vapoursynth_script", fake_check)

    result = runner.invoke(
        app,
        [
            "plan",
            str(movie),
            "--profile",
            "av1_1080p_sdr",
            "--check-vpy",
            "--config",
            str(config_path),
        ],
    )

    assert result.exit_code == 0
    assert calls == [_artifact_dir_from_output(result.output) / "movie.vpy"]
    assert "runtime:    PASS" in result.output


def test_runtime_validation_failure_keeps_generated_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, movie = _tracked_movie(tmp_path)
    monkeypatch.setattr("avarch.cli.run_ffprobe", _fake_sdr_ffprobe)
    _probe(config_path, movie)

    def fail_check(_script_path: Path) -> object:
        raise VspipeProcessError("bounded stdout/stderr excerpt")

    monkeypatch.setattr("avarch.cli.check_vapoursynth_script", fail_check)

    result = runner.invoke(
        app,
        [
            "plan",
            str(movie),
            "--profile",
            "av1_1080p_sdr",
            "--check-vpy",
            "--config",
            str(config_path),
        ],
    )

    artifact_dir = _plan_dir(config_path)
    assert result.exit_code != 0
    assert "runtime validation failed" in result.output
    assert "bounded stdout/stderr excerpt" in result.output
    assert (artifact_dir / "movie.vpy").is_file()


def test_existing_artifact_conflict_workflow_blocks_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, movie = _tracked_movie(tmp_path)
    monkeypatch.setattr("avarch.cli.run_ffprobe", _fake_sdr_ffprobe)
    _probe(config_path, movie)

    first = runner.invoke(
        app,
        ["plan", str(movie), "--profile", "av1_1080p_sdr", "--config", str(config_path)],
    )
    artifact_dir = _artifact_dir_from_output(first.output)
    (artifact_dir / "movie.vpy").write_text("# user changed artifact\n", encoding="utf-8")
    second = runner.invoke(
        app,
        ["plan", str(movie), "--profile", "av1_1080p_sdr", "--config", str(config_path)],
    )

    assert first.exit_code == 0
    assert second.exit_code != 0
    assert "Plan artifact bundle already exists" in second.output
    assert (artifact_dir / "movie.vpy").read_text(encoding="utf-8") == "# user changed artifact\n"


def test_custom_template_workflow_supports_hdr_and_preserves_user_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template_path = tmp_path / "templates" / "custom.vpy"
    template_body = (
        "OTHER_SOURCE = '/tmp/not-the-planned-source.mkv'\n"
        "clip = object()\n"
        "clip.set_output(index=0)\n"
    )
    template_path.parent.mkdir()
    template_path.write_text(template_body, encoding="utf-8")
    config_path, movie = _tracked_movie(tmp_path)
    _write_user_profile(
        config_path.parent / "profiles" / "custom_template.toml",
        name="custom_template",
        extra='vapoursynth_template = "../templates/custom.vpy"\n',
    )
    monkeypatch.setattr("avarch.cli.run_ffprobe", _fake_hdr_ffprobe)
    _probe(config_path, movie)

    result = runner.invoke(
        app,
        ["plan", str(movie), "--profile", "custom_template", "--config", str(config_path)],
    )

    artifact_dir = _artifact_dir_from_output(result.output)
    script = (artifact_dir / "movie.vpy").read_text(encoding="utf-8")
    plan = json.loads((artifact_dir / "plan.json").read_text(encoding="utf-8"))

    assert result.exit_code == 0
    assert plan["vapoursynth"]["mode"] == "custom_template"
    assert plan["vapoursynth"]["source_color_transfer"] == "smpte2084"
    assert "AVARCH_SOURCE_PATH" in script
    assert template_body in script
    assert "OTHER_SOURCE = '/tmp/not-the-planned-source.mkv'" in script


def test_multiple_config_workflow_isolates_databases_and_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_dir = tmp_path / "first"
    second_dir = tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    movie = tmp_path / "shared.mkv"
    movie.write_bytes(b"media")
    monkeypatch.setattr("avarch.cli.run_ffprobe", _fake_sdr_ffprobe)

    first_config = _init_config(first_dir)
    second_config = _init_config(second_dir)
    _scan_probe(first_config, movie.parent, movie)
    _scan_probe(second_config, movie.parent, movie)

    first_plan = runner.invoke(
        app,
        ["plan", str(movie), "--profile", "av1_1080p_sdr", "--config", str(first_config)],
    )
    second_plan = runner.invoke(
        app,
        ["plan", str(movie), "--profile", "av1_1080p_sdr", "--config", str(second_config)],
    )

    assert first_plan.exit_code == 0
    assert second_plan.exit_code == 0
    assert _artifact_dir_from_output(first_plan.output) != _artifact_dir_from_output(
        second_plan.output
    )


def _fake_sdr_ffprobe(_path: Path) -> dict[str, Any]:
    return sdr_probe_payload()


def _fake_hdr_ffprobe(_path: Path) -> dict[str, Any]:
    return representative_probe_payload()


def _init_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "avarch.toml"
    result = runner.invoke(app, ["init", "--config", str(config_path)])
    assert result.exit_code == 0
    return config_path


def _write_inventory_config(tmp_path: Path, *, roots: list[Path]) -> Path:
    config_path = tmp_path / "avarch.toml"
    roots_text = ", ".join(f'"{root}"' for root in roots)
    config_path.write_text(
        f"""
[app]
data_dir = ".avarch"

[database]
url = "sqlite:///.avarch/avarch.db"

[logging]
level = "INFO"
format = "console"

[scanner]
roots = [{roots_text}]
extensions = ["mkv", ".mp4"]
exclude_directories = [".avarch-work"]
""".strip(),
        encoding="utf-8",
    )
    return config_path


def _tracked_movie(tmp_path: Path) -> tuple[Path, Path]:
    config_path = _init_config(tmp_path)
    media_root = tmp_path / "media"
    media_root.mkdir()
    movie = media_root / "movie.mkv"
    movie.write_bytes(b"media")
    scan_result = runner.invoke(app, ["scan", str(media_root), "--config", str(config_path)])
    assert scan_result.exit_code == 0
    return config_path, movie


def _scan_probe(config_path: Path, media_root: Path, movie: Path) -> None:
    scan_result = runner.invoke(app, ["scan", str(media_root), "--config", str(config_path)])
    assert scan_result.exit_code == 0
    _probe(config_path, movie)


def _probe(config_path: Path, movie: Path) -> None:
    probe_result = runner.invoke(app, ["probe", str(movie), "--config", str(config_path)])
    assert probe_result.exit_code == 0


def _artifact_dir_from_output(output: str) -> Path:
    for line in output.splitlines():
        if line.startswith("Artifact dir: "):
            return Path(line.removeprefix("Artifact dir: "))
    raise AssertionError("plan output did not contain an artifact directory")


def _plan_dir(config_path: Path) -> Path:
    plans_dir = config_path.parent / ".avarch" / "plans"
    plan_dirs = list(plans_dir.iterdir())
    assert len(plan_dirs) == 1
    return plan_dirs[0]


def _replace_config_line(config_path: Path, old: str, new: str) -> None:
    text = config_path.read_text(encoding="utf-8")
    assert old in text
    config_path.write_text(text.replace(old, new), encoding="utf-8")


def _write_user_profile(profile_path: Path, *, name: str, extra: str = "") -> None:
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(
        f"""
schema_version = 1
name = "{name}"

backend = "av1an"
container = "mkv"
{extra}
[match]
video_codec_not = ["av1"]

[video]
max_width = 1920
hdr_to_sdr = true
source = "vapoursynth"

[av1an]
encoder = "svt-av1"
workers = 6
video_args = "--preset 6 --crf 28 --keyint 240"

[audio]
codec = "libopus"
bitrate = "128k"
channels = 2
languages = ["eng"]

[subtitles]
languages = ["eng"]
keep_forced = true
""".strip(),
        encoding="utf-8",
    )
