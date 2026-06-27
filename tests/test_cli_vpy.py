from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from avarch.cli import app
from tests.probe_fixtures import sdr_probe_payload

runner = CliRunner()


def test_vpy_validate_accepts_workspace_custom_filter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path, monkeypatch)
    _write_profile(
        tmp_path,
        name="filtered",
        vapoursynth_block="""
[vapoursynth]
mode = "custom_filter"
script = "my_filter.py"
entrypoint = "apply"
api_version = 1
""",
    )
    (tmp_path / ".avarch" / "scripts" / "my_filter.py").write_text(
        """
from __future__ import annotations

import vapoursynth as vs

from avarch.vpy_api import FilterContext


def apply(video: vs.VideoNode, context: FilterContext) -> vs.VideoNode:
    del context
    return video
""".strip()
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["vpy", "validate", "--profile", "filtered"])

    assert result.exit_code == 0
    assert "Static VapourSynth validation passed" in result.output


def test_vpy_validate_rejects_script_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path, monkeypatch)
    _write_profile(
        tmp_path,
        name="escape",
        vapoursynth_block="""
[vapoursynth]
mode = "custom_filter"
script = "../outside.py"
entrypoint = "apply"
api_version = 1
""",
    )

    result = runner.invoke(app, ["vpy", "validate", "--profile", "escape"])

    assert result.exit_code != 0
    assert "escapes the workspace scripts directory" in result.output


def test_vpy_validate_accepts_workspace_custom_template(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path, monkeypatch)
    _write_profile(
        tmp_path,
        name="templated",
        vapoursynth_block="""
[vapoursynth]
mode = "custom_template"
template = "my_pipeline.vpy"
api_version = 1
""",
    )
    (tmp_path / ".avarch" / "scripts" / "my_pipeline.vpy").write_text(
        "clip = 1\nclip.set_output(index=0)\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["vpy", "validate", "--profile", "templated"])

    assert result.exit_code == 0


def test_vpy_check_generates_and_evaluates_script(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path, monkeypatch)
    media_dir = tmp_path / "Movies"
    media_dir.mkdir()
    movie = media_dir / "Test.mkv"
    movie.write_bytes(b"media")
    calls: list[Path] = []

    monkeypatch.setattr("avarch.bootstrap.run_ffprobe", _fake_ffprobe)

    def fake_check(script_path: Path, **_kwargs: object) -> None:
        calls.append(script_path)

    monkeypatch.setattr("avarch.bootstrap.check_vapoursynth_script", fake_check)
    scan_result = runner.invoke(app, ["scan", "."])
    probe_result = runner.invoke(app, ["probe", "--file", "Movies/Test.mkv"])

    result = runner.invoke(app, ["vpy", "check", "--profile", "default", "Movies/Test.mkv"])

    assert scan_result.exit_code == 0
    assert probe_result.exit_code == 0
    assert result.exit_code == 0
    assert "VapourSynth runtime check passed" in result.output
    assert calls
    assert calls[0].name == "Test.vpy"


def test_vpy_check_custom_filter_snapshots_filter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _init_workspace(tmp_path, monkeypatch)
    media_dir = tmp_path / "Movies"
    media_dir.mkdir()
    movie = media_dir / "Test.mkv"
    movie.write_bytes(b"media")
    _write_profile(
        tmp_path,
        name="filtered",
        vapoursynth_block="""
[vapoursynth]
mode = "custom_filter"
script = "my_filter.py"
entrypoint = "apply"
api_version = 1
""",
    )
    filter_text = "def apply(video, context):\n    return video\n"
    (tmp_path / ".avarch" / "scripts" / "my_filter.py").write_text(
        filter_text,
        encoding="utf-8",
    )
    monkeypatch.setattr("avarch.bootstrap.run_ffprobe", _fake_ffprobe)

    def fake_check(_script_path: Path, **_kwargs: object) -> None:
        return None

    monkeypatch.setattr("avarch.bootstrap.check_vapoursynth_script", fake_check)
    scan_result = runner.invoke(app, ["scan", "."])
    probe_result = runner.invoke(app, ["probe", "--file", "Movies/Test.mkv"])

    result = runner.invoke(app, ["vpy", "check", "--profile", "filtered", "Movies/Test.mkv"])

    script_path = _script_path_from_output(result.output)
    artifact_dir = script_path.parent
    assert scan_result.exit_code == 0
    assert probe_result.exit_code == 0
    assert result.exit_code == 0
    assert (artifact_dir / "vpy" / "user_filter.py").read_text(encoding="utf-8") == filter_text
    assert "filter_path = Path(" in script_path.read_text(encoding="utf-8")


def _init_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PWD", str(tmp_path))
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0


def _write_profile(
    workspace: Path,
    *,
    name: str,
    vapoursynth_block: str,
) -> None:
    profile_path = workspace / ".avarch" / "profiles" / f"{name}.toml"
    profile_path.write_text(
        f"""
schema_version = 1
name = "{name}"

backend = "av1an"
container = "mkv"
{vapoursynth_block}
[match]
video_codec_not = ["av1"]

[video]
max_width = 1920
hdr_to_sdr = true
source = "vapoursynth"

[av1an]
encoder = "svt-av1"
workers = 2
video_args = "--preset 6 --crf 28 --keyint 240 --lp 2"

[audio]
codec = "libopus"
bitrate = "128k"
channels = 2
languages = ["eng"]

[subtitles]
languages = ["eng"]
keep_forced = true
""".strip()
        + "\n",
        encoding="utf-8",
    )


def _fake_ffprobe(_path: Path) -> dict[str, Any]:
    return sdr_probe_payload()


def _script_path_from_output(output: str) -> Path:
    for line in output.splitlines():
        if line.startswith("Script: "):
            return Path(line.removeprefix("Script: "))
    raise AssertionError("output did not contain script path")
