from pathlib import Path
from typing import Any

import pytest

from avarch.adapters.vapoursynth import (
    DEFAULT_VSPIPE_TIMEOUT_SECONDS,
    VspipeProcessError,
    build_bounded_output_excerpt,
    build_vspipe_info_command,
    check_vapoursynth_script,
)


def test_vspipe_command_is_argument_list() -> None:
    command = build_vspipe_info_command(Path("/tmp/movie file.vpy"))

    assert command == ["vspipe", "--info", "/tmp/movie file.vpy", "-"]


def test_default_timeout_is_documented_constant() -> None:
    assert DEFAULT_VSPIPE_TIMEOUT_SECONDS == 600.0


def test_successful_check_returns_bounded_output(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    class Result:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(command: list[str], **kwargs: object) -> Result:
        calls.append({"command": command, **kwargs})
        return Result()

    monkeypatch.setattr("avarch.adapters.vapoursynth.subprocess.run", fake_run)

    result = check_vapoursynth_script(Path("/tmp/movie file.vpy"))

    assert result.stdout_excerpt == "ok"
    assert calls[0]["command"] == ["vspipe", "--info", "/tmp/movie file.vpy", "-"]
    assert calls[0]["timeout"] == DEFAULT_VSPIPE_TIMEOUT_SECONDS


def test_check_passes_environment_to_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[dict[str, Any]] = []

    class Result:
        returncode = 0
        stdout = "ok"
        stderr = ""

    def fake_run(command: list[str], **kwargs: object) -> Result:
        calls.append({"command": command, **kwargs})
        return Result()

    monkeypatch.setattr("avarch.adapters.vapoursynth.subprocess.run", fake_run)

    check_vapoursynth_script(Path("/tmp/movie.vpy"), env={"PYTHONPATH": "/workspace/scripts"})

    assert calls[0]["env"] == {"PYTHONPATH": "/workspace/scripts"}


def test_nonzero_exit_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    class Result:
        returncode = 1
        stdout = "head" + ("x" * 9000) + "tail"
        stderr = "error"

    def fake_run(_command: list[str], **_kwargs: object) -> Result:
        return Result()

    monkeypatch.setattr("avarch.adapters.vapoursynth.subprocess.run", fake_run)

    with pytest.raises(VspipeProcessError) as exc_info:
        check_vapoursynth_script(Path("/tmp/movie.vpy"))

    message = str(exc_info.value)
    assert "vspipe failed with exit code 1" in message
    assert "output truncated" in message
    assert "head" in message
    assert "tail" in message


def test_short_output_is_not_modified() -> None:
    assert build_bounded_output_excerpt("short", max_bytes=10) == "short"


def test_long_output_contains_truncation_marker_and_is_bounded() -> None:
    excerpt = build_bounded_output_excerpt("abcd" * 1000, max_bytes=100)

    assert "output truncated" in excerpt
    assert len(excerpt.encode("utf-8")) <= 100
