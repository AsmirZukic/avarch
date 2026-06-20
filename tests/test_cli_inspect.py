from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlmodel import Session, select
from typer.testing import CliRunner

from avarch.cli import app
from avarch.config import load_config, resolve_database_url
from avarch.db import create_db_engine
from avarch.models.db import MediaFile, MediaFileStatus
from avarch.models.probe import NormalizedProbe
from avarch.probe import store_probe_result

runner = CliRunner()


def test_inspect_command_prints_latest_probe(tmp_path: Path) -> None:
    config_path = _init_config(tmp_path)
    media_file = _insert_media_file(config_path, tmp_path / "movie.mkv")
    _insert_probe(config_path, media_file, NormalizedProbe(container="matroska,webm"))

    result = runner.invoke(app, ["inspect", str(media_file)])

    assert result.exit_code == 0
    assert "Container: matroska,webm" in result.output


def test_inspect_uses_latest_probe_pointer(tmp_path: Path) -> None:
    config_path = _init_config(tmp_path)
    media_file = _insert_media_file(config_path, tmp_path / "movie.mkv")
    first_probe_id = _insert_probe(config_path, media_file, NormalizedProbe(duration_seconds=1.0))
    _insert_probe(
        config_path,
        media_file,
        NormalizedProbe(duration_seconds=2.0),
        created_at=datetime(2026, 6, 14, tzinfo=UTC) + timedelta(seconds=1),
    )
    _set_latest_probe_id(config_path, media_file, first_probe_id)

    result = runner.invoke(app, ["inspect", str(media_file)])

    assert result.exit_code == 0
    assert "Duration: 1 s" in result.output


def test_inspect_does_not_execute_ffprobe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = _init_config(tmp_path)
    media_file = _insert_media_file(config_path, tmp_path / "movie.mkv")
    _insert_probe(config_path, media_file, NormalizedProbe())

    def fail_probe(_path: Path) -> dict[str, object]:
        raise AssertionError("inspect must not execute ffprobe")

    monkeypatch.setattr("avarch.cli.run_ffprobe", fail_probe)

    result = runner.invoke(app, ["inspect", str(media_file)])

    assert result.exit_code == 0


def test_inspect_works_when_media_file_is_missing(tmp_path: Path) -> None:
    config_path = _init_config(tmp_path)
    media_file = _insert_media_file(
        config_path,
        tmp_path / "movie.mkv",
        status=MediaFileStatus.MISSING,
    )
    media_file.unlink()
    _insert_probe(config_path, media_file, NormalizedProbe(container="matroska,webm"))

    result = runner.invoke(app, ["inspect", str(media_file)])

    assert result.exit_code == 0
    assert "Container: matroska,webm" in result.output


def test_inspect_fails_without_probe_result(tmp_path: Path) -> None:
    config_path = _init_config(tmp_path)
    media_file = _insert_media_file(config_path, tmp_path / "movie.mkv")

    result = runner.invoke(app, ["inspect", str(media_file)])

    assert result.exit_code != 0
    assert "No stored probe result" in result.output


def test_inspect_fails_for_unknown_file(tmp_path: Path) -> None:
    _init_config(tmp_path)

    result = runner.invoke(
        app,
        ["inspect", str(tmp_path / "unknown.mkv")],
    )

    assert result.exit_code != 0
    assert "File is not present in the media inventory." in result.output


def _init_config(tmp_path: Path) -> Path:
    config_path = tmp_path / ".avarch" / "config.toml"
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    return config_path


def _insert_media_file(
    config_path: Path,
    path: Path,
    *,
    status: MediaFileStatus = MediaFileStatus.PRESENT,
) -> Path:
    path.write_bytes(b"media")
    stat_result = path.stat()
    engine = _engine(config_path)
    now = datetime(2026, 6, 14, tzinfo=UTC)
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
                status=status,
            )
        )
        session.commit()
    return path


def _insert_probe(
    config_path: Path,
    media_file: Path,
    normalized_probe: NormalizedProbe,
    *,
    created_at: datetime = datetime(2026, 6, 14, tzinfo=UTC),
) -> int:
    engine = _engine(config_path)
    with Session(engine) as session:
        stored_media_file = session.exec(
            select(MediaFile).where(MediaFile.path == str(media_file.resolve()))
        ).one()
        probe_result = store_probe_result(
            session,
            media_file=stored_media_file,
            raw_probe={"format": {}},
            normalized_probe=normalized_probe,
            created_at=created_at,
        )
        session.commit()
        session.refresh(probe_result)
        return probe_result.id or 0


def _set_latest_probe_id(config_path: Path, media_file: Path, probe_result_id: int) -> None:
    engine = _engine(config_path)
    with Session(engine) as session:
        stored_media_file = session.exec(
            select(MediaFile).where(MediaFile.path == str(media_file.resolve()))
        ).one()
        stored_media_file.latest_probe_id = probe_result_id
        session.add(stored_media_file)
        session.commit()


def _engine(config_path: Path) -> Engine:
    app_config = load_config(config_path)
    return create_db_engine(resolve_database_url(app_config, config_path))
