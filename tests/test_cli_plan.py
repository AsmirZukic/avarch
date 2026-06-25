from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine
from sqlmodel import Session, select
from typer.testing import CliRunner

from avarch.adapters.probe import normalize_probe
from avarch.adapters.sqlite.db import create_db_engine
from avarch.adapters.sqlite.models import MediaFile, MediaFileStatus
from avarch.adapters.sqlite.probes import store_probe_result
from avarch.adapters.sqlite.urls import resolve_database_url
from avarch.cli import app
from avarch.config import load_config
from avarch.models.probe import AudioStream, NormalizedProbe, VideoStream
from tests.probe_fixtures import representative_probe_payload, sdr_probe_payload

runner = CliRunner()


def test_plan_command_creates_bundle(tmp_path: Path) -> None:
    config_path = _init_config(tmp_path)
    media_file = _tracked_file(config_path, tmp_path / "movie.mkv")
    _store_probe(config_path, media_file)

    result = runner.invoke(
        app,
        ["plan", "--profile", "av1_1080p_sdr", "--file", str(media_file)],
    )

    assert result.exit_code == 0
    artifact_dir = _artifact_dir_from_output(result.output)
    assert (artifact_dir / "plan.json").is_file()
    assert (artifact_dir / "av1an.command.json").is_file()
    assert (artifact_dir / "validation-policy.json").is_file()
    assert (artifact_dir / "movie.vpy").is_file()


def test_plan_command_uses_packaged_builtin_when_workspace_profiles_dir_is_missing(
    tmp_path: Path,
) -> None:
    config_path = _init_config(tmp_path)
    profiles_dir = tmp_path / ".avarch" / "profiles"
    shutil.rmtree(profiles_dir)
    media_file = _tracked_file(config_path, tmp_path / "movie.mkv")
    _store_probe(config_path, media_file)

    result = runner.invoke(
        app,
        ["plan", "--profile", "av1_1080p_sdr", "--file", str(media_file)],
    )

    assert result.exit_code == 0
    assert "Profile:      av1_1080p_sdr" in result.output
    assert not (profiles_dir / "av1_1080p_sdr.toml").exists()


def test_plan_command_does_not_run_vspipe_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _init_config(tmp_path)
    media_file = _tracked_file(config_path, tmp_path / "movie.mkv")
    _store_probe(config_path, media_file)

    def fail_check(_path: Path, **_kwargs: object) -> object:
        raise AssertionError("plan must not run vspipe unless --check-vpy is requested")

    monkeypatch.setattr("avarch.cli.check_vapoursynth_script", fail_check)

    result = runner.invoke(
        app,
        ["plan", "--profile", "av1_1080p_sdr", "--file", str(media_file)],
    )

    assert result.exit_code == 0


def test_plan_command_check_vpy_validates_bestsource_script(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _init_config(tmp_path)
    media_file = _tracked_file(config_path, tmp_path / "movie.mkv")
    _store_probe(config_path, media_file)
    calls: list[Path] = []

    def fake_check(script_path: Path, **_kwargs: object) -> object:
        calls.append(script_path)
        return object()

    monkeypatch.setattr("avarch.cli.check_vapoursynth_script", fake_check)

    result = runner.invoke(
        app,
        [
            "plan",
            "--profile",
            "av1_1080p_sdr",
            "--file",
            str(media_file),
            "--check-vpy",
        ],
    )

    artifact_dir = _artifact_dir_from_output(result.output)
    script_path = artifact_dir / "movie.vpy"
    script = script_path.read_text(encoding="utf-8")

    assert result.exit_code == 0
    assert calls == [script_path]
    assert "core.bs.VideoSource" in script
    assert "core.lsmas" not in script
    assert "LWLibavSource" not in script
    assert "runtime:    PASS" in result.output


def test_plan_command_prints_summary(tmp_path: Path) -> None:
    config_path = _init_config(tmp_path)
    media_file = _tracked_file(config_path, tmp_path / "movie.mkv")
    _store_probe(config_path, media_file)

    result = runner.invoke(
        app,
        ["plan", "--profile", "av1_1080p_sdr", "--file", str(media_file)],
    )

    assert result.exit_code == 0
    assert "Plan hash:" in result.output
    assert "source:     hevc 3840x2160 yuv420p10le" in result.output
    assert "Audio: [1] eac3 eng -> libopus 128k 2ch" in result.output
    assert "syntax:     PASS" in result.output
    assert "runtime:    not checked" in result.output


def test_plan_command_executes_no_external_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _init_config(tmp_path)
    media_file = _tracked_file(config_path, tmp_path / "movie.mkv")
    _store_probe(config_path, media_file)

    def fail_probe(_path: Path) -> dict[str, Any]:
        raise AssertionError("plan must not execute ffprobe")

    monkeypatch.setattr("avarch.cli.run_ffprobe", fail_probe)

    result = runner.invoke(
        app,
        ["plan", "--profile", "av1_1080p_sdr", "--file", str(media_file)],
    )

    assert result.exit_code == 0


def test_repeated_plan_command_is_idempotent(tmp_path: Path) -> None:
    config_path = _init_config(tmp_path)
    media_file = _tracked_file(config_path, tmp_path / "movie.mkv")
    _store_probe(config_path, media_file)
    command = ["plan", "--profile", "av1_1080p_sdr", "--file", str(media_file)]

    first = runner.invoke(app, command)
    second = runner.invoke(app, command)

    assert first.exit_code == 0
    assert second.exit_code == 0
    assert "Processed: 1" in first.output
    assert "plan-current" in second.output
    assert "Processed: 0" in second.output


def test_plan_command_rejects_av1_input(tmp_path: Path) -> None:
    config_path = _init_config(tmp_path)
    media_file = _tracked_file(config_path, tmp_path / "movie.mkv")
    _store_normalized_probe(
        config_path,
        media_file,
        NormalizedProbe(
            video_streams=[
                VideoStream(index=0, codec="av1", width=1920, height=1080),
            ],
            audio_streams=[
                AudioStream(index=1, codec="eac3", language="eng", channels=6),
            ],
        ),
    )

    result = runner.invoke(
        app,
        ["plan", "--profile", "av1_1080p_sdr", "--file", str(media_file)],
    )

    assert result.exit_code != 0
    assert "video codec is excluded: av1" in result.output


def test_plan_command_accepts_builtin_hdr_source_with_hdr_to_sdr(tmp_path: Path) -> None:
    config_path = _init_config(tmp_path)
    media_file = _tracked_file(config_path, tmp_path / "movie.mkv")
    _store_normalized_probe(
        config_path,
        media_file,
        normalize_probe(representative_probe_payload()),
        raw_probe=representative_probe_payload(),
    )

    result = runner.invoke(
        app,
        ["plan", "--profile", "av1_1080p_sdr", "--file", str(media_file)],
    )

    assert result.exit_code == 0
    assert "Plan hash:" in result.output


def test_plan_command_falls_back_to_available_audio_when_language_does_not_match(
    tmp_path: Path,
) -> None:
    config_path = _init_config(tmp_path)
    media_file = _tracked_file(config_path, tmp_path / "movie.mkv")
    _store_normalized_probe(
        config_path,
        media_file,
        NormalizedProbe(
            duration_seconds=600.0,
            video_streams=[
                VideoStream(
                    index=0,
                    codec="hevc",
                    width=1920,
                    height=1080,
                    pix_fmt="yuv420p10le",
                ),
            ],
            audio_streams=[
                AudioStream(index=1, codec="eac3", language="jpn", channels=6),
            ],
        ),
    )

    result = runner.invoke(
        app,
        ["plan", "--profile", "av1_1080p_sdr", "--file", str(media_file)],
    )

    assert result.exit_code == 0
    assert "Audio: [1] eac3 jpn -> libopus 128k 2ch" in result.output


def test_plan_command_allows_sources_without_audio_streams(tmp_path: Path) -> None:
    config_path = _init_config(tmp_path)
    media_file = _tracked_file(config_path, tmp_path / "movie.mkv")
    _store_normalized_probe(
        config_path,
        media_file,
        NormalizedProbe(
            duration_seconds=600.0,
            video_streams=[
                VideoStream(
                    index=0,
                    codec="hevc",
                    width=1920,
                    height=1080,
                    pix_fmt="yuv420p10le",
                ),
            ],
            audio_streams=[],
        ),
    )

    result = runner.invoke(
        app,
        ["plan", "--profile", "av1_1080p_sdr", "--file", str(media_file)],
    )

    assert result.exit_code == 0
    assert "Audio: none" in result.output


def _init_config(tmp_path: Path) -> Path:
    config_path = tmp_path / ".avarch" / "config.toml"
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    return config_path


def _tracked_file(config_path: Path, path: Path) -> Path:
    path.write_bytes(b"media")
    stat_result = path.stat()
    now = datetime(2026, 6, 14, tzinfo=UTC)
    engine = _engine(config_path)
    with Session(engine) as session:
        session.add(
            MediaFile(
                path=str(path.resolve()),
                size_bytes=stat_result.st_size,
                mtime_ns=stat_result.st_mtime_ns,
                device_id=stat_result.st_dev,
                inode=stat_result.st_ino,
                fs_fingerprint="key",
                discovered_at=now,
                last_seen_at=now,
                status=MediaFileStatus.PRESENT,
            )
        )
        session.commit()
    return path


def _store_probe(config_path: Path, media_file: Path) -> None:
    payload = sdr_probe_payload()
    _store_normalized_probe(
        config_path,
        media_file,
        normalize_probe(payload),
        raw_probe=payload,
    )


def _store_normalized_probe(
    config_path: Path,
    media_file: Path,
    normalized_probe: NormalizedProbe,
    *,
    raw_probe: dict[str, Any] | None = None,
) -> None:
    engine = _engine(config_path)
    with Session(engine) as session:
        stored_media_file = session.exec(
            select(MediaFile).where(MediaFile.path == str(media_file.resolve()))
        ).one()
        store_probe_result(
            session,
            media_file=stored_media_file,
            raw_probe=raw_probe or {"format": {}},
            normalized_probe=normalized_probe,
            created_at=datetime(2026, 6, 14, tzinfo=UTC),
        )
        session.commit()


def _artifact_dir_from_output(output: str) -> Path:
    for line in output.splitlines():
        if line.startswith("Artifact dir: "):
            return Path(line.removeprefix("Artifact dir: "))
    raise AssertionError("plan output did not contain an artifact directory")


def _engine(config_path: Path) -> Engine:
    app_config = load_config(config_path)
    return create_db_engine(resolve_database_url(app_config, config_path))
