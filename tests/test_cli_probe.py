from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine
from sqlmodel import Session, select
from typer.testing import CliRunner

from avarch.cli import app
from avarch.config import load_config, resolve_database_url
from avarch.db import create_db_engine
from avarch.models.db import MediaFile, MediaFileStatus, ProbeResult
from avarch.probe import ProbeProcessError
from tests.probe_fixtures import representative_probe_payload

runner = CliRunner()


def test_probe_command_stores_result_for_tracked_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _init_config(tmp_path)
    media_file = _tracked_file(tmp_path, config_path)
    monkeypatch.setattr("avarch.cli.run_ffprobe", _fake_ffprobe)

    result = runner.invoke(app, ["probe", str(media_file), "--config", str(config_path)])

    assert result.exit_code == 0
    assert _probe_result_count(config_path) == 1


def test_probe_command_prints_normalized_summary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _init_config(tmp_path)
    media_file = _tracked_file(tmp_path, config_path)
    monkeypatch.setattr("avarch.cli.run_ffprobe", _fake_ffprobe)

    result = runner.invoke(app, ["probe", str(media_file), "--config", str(config_path)])

    assert result.exit_code == 0
    assert "Container: matroska,webm" in result.output
    assert "[0] hevc 3840x2160 24000/1001 fps 10-bit yuv420p10le" in result.output
    assert "Probe hash:" in result.output


def test_probe_command_rejects_untracked_file(tmp_path: Path) -> None:
    config_path = _init_config(tmp_path)
    media_file = tmp_path / "movie.mkv"
    media_file.write_bytes(b"not tracked")

    result = runner.invoke(app, ["probe", str(media_file), "--config", str(config_path)])

    assert result.exit_code != 0
    assert "File is not present in the media inventory." in result.output


def test_probe_command_rejects_missing_input_path(tmp_path: Path) -> None:
    config_path = _init_config(tmp_path)

    result = runner.invoke(
        app,
        ["probe", str(tmp_path / "missing.mkv"), "--config", str(config_path)],
    )

    assert result.exit_code != 0
    assert "File does not exist" in result.output


def test_probe_command_rejects_directory(tmp_path: Path) -> None:
    config_path = _init_config(tmp_path)

    result = runner.invoke(app, ["probe", str(tmp_path), "--config", str(config_path)])

    assert result.exit_code != 0
    assert "Path is not a regular file" in result.output


def test_probe_command_rejects_missing_inventory_status(tmp_path: Path) -> None:
    config_path = _init_config(tmp_path)
    media_file = _tracked_file(tmp_path, config_path, status=MediaFileStatus.MISSING)

    result = runner.invoke(app, ["probe", str(media_file), "--config", str(config_path)])

    assert result.exit_code != 0
    assert "File is marked missing" in result.output


def test_probe_command_does_not_store_failed_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _init_config(tmp_path)
    media_file = _tracked_file(tmp_path, config_path)

    def fail_probe(_path: Path) -> dict[str, Any]:
        raise ProbeProcessError("ffprobe failed")

    monkeypatch.setattr("avarch.cli.run_ffprobe", fail_probe)

    result = runner.invoke(app, ["probe", str(media_file), "--config", str(config_path)])

    assert result.exit_code != 0
    assert _probe_result_count(config_path) == 0


def test_probe_command_does_not_mutate_media_file_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _init_config(tmp_path)
    media_file = _tracked_file(tmp_path, config_path, status=MediaFileStatus.ADDED)
    before = _load_media_file(config_path, media_file)
    monkeypatch.setattr("avarch.cli.run_ffprobe", _fake_ffprobe)

    result = runner.invoke(app, ["probe", str(media_file), "--config", str(config_path)])

    after = _load_media_file(config_path, media_file)
    assert result.exit_code == 0
    assert after.status == before.status
    assert after.last_seen_at == before.last_seen_at
    assert after.content_key == before.content_key


def _fake_ffprobe(_path: Path) -> dict[str, Any]:
    return representative_probe_payload()


def _init_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "avarch.toml"
    result = runner.invoke(app, ["init", "--config", str(config_path)])
    assert result.exit_code == 0
    return config_path


def _tracked_file(
    tmp_path: Path,
    config_path: Path,
    *,
    status: MediaFileStatus = MediaFileStatus.PRESENT,
) -> Path:
    media_file = tmp_path / "movie.mkv"
    media_file.write_bytes(b"media")
    _insert_media_file(config_path, media_file, status)
    return media_file


def _insert_media_file(config_path: Path, path: Path, status: MediaFileStatus) -> None:
    engine = _engine(config_path)
    now = datetime(2026, 6, 14, tzinfo=UTC)
    with Session(engine) as session:
        session.add(
            MediaFile(
                path=str(path.resolve()),
                size_bytes=path.stat().st_size,
                mtime_ns=path.stat().st_mtime_ns,
                device_id=path.stat().st_dev,
                inode=path.stat().st_ino,
                content_key="key",
                discovered_at=now,
                last_seen_at=now,
                status=status,
            )
        )
        session.commit()


def _probe_result_count(config_path: Path) -> int:
    engine = _engine(config_path)
    with Session(engine) as session:
        return len(session.exec(select(ProbeResult)).all())


def _load_media_file(config_path: Path, path: Path) -> MediaFile:
    engine = _engine(config_path)
    with Session(engine) as session:
        return session.exec(select(MediaFile).where(MediaFile.path == str(path.resolve()))).one()


def _engine(config_path: Path) -> Engine:
    app_config = load_config(config_path)
    return create_db_engine(resolve_database_url(app_config, config_path))
