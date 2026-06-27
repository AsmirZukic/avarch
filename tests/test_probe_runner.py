from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from avarch.adapters.probe import (
    ProbeExecutableNotFoundError,
    ProbeOutputError,
    ProbeProcessError,
    ProbeTimeoutError,
    build_ffprobe_command,
    run_ffprobe,
)


def test_build_ffprobe_command_contains_json_output() -> None:
    command = build_ffprobe_command(Path("/media/movie.mkv"))

    assert "-print_format" in command
    assert "json" in command


def test_build_ffprobe_command_requests_format_streams_and_chapters() -> None:
    command = build_ffprobe_command(Path("/media/movie.mkv"))

    assert "-show_format" in command
    assert "-show_streams" in command
    assert "-show_chapters" in command


def test_build_ffprobe_command_preserves_path_as_one_argument(tmp_path: Path) -> None:
    path = tmp_path / "movie with spaces.mkv"

    command = build_ffprobe_command(path)

    assert command[-1] == str(path)


def test_build_ffprobe_command_supports_custom_executable() -> None:
    command = build_ffprobe_command(Path("/media/movie.mkv"), executable="/opt/bin/ffprobe")

    assert command[0] == "/opt/bin/ffprobe"


def test_run_ffprobe_returns_json_object(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["ffprobe"], 0, stdout='{"format":{}}', stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert run_ffprobe(tmp_path / "movie.mkv") == {"format": {}}


def test_run_ffprobe_raises_for_missing_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ProbeExecutableNotFoundError):
        run_ffprobe(tmp_path / "movie.mkv")


def test_run_ffprobe_raises_for_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(["ffprobe"], timeout=1)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ProbeTimeoutError):
        run_ffprobe(tmp_path / "movie.mkv", timeout_seconds=1)


def test_run_ffprobe_raises_for_nonzero_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["ffprobe"], 1, stdout="", stderr="broken")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ProbeProcessError, match="broken"):
        run_ffprobe(tmp_path / "movie.mkv")


def test_run_ffprobe_raises_for_invalid_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["ffprobe"], 0, stdout="{", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ProbeOutputError):
        run_ffprobe(tmp_path / "movie.mkv")


def test_run_ffprobe_rejects_json_array(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["ffprobe"], 0, stdout="[]", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(ProbeOutputError):
        run_ffprobe(tmp_path / "movie.mkv")


def test_run_ffprobe_invokes_subprocess_without_shell(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_kwargs: dict[str, Any] = {}

    def fake_run(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed_kwargs.update(kwargs)
        return subprocess.CompletedProcess(["ffprobe"], 0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    run_ffprobe(tmp_path / "movie.mkv")

    assert observed_kwargs["check"] is False
    assert "shell" not in observed_kwargs
