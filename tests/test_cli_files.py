from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session
from typer.testing import CliRunner

from avarch.cli import app
from avarch.config import load_config, resolve_database_url
from avarch.db import create_db_engine
from avarch.models.db import MediaFile, MediaFileStatus

runner = CliRunner()


def test_files_command_lists_inventory(tmp_path: Path) -> None:
    config_path = _init_config(tmp_path)
    _insert_media_file(config_path, "/media/movie.mkv", MediaFileStatus.PRESENT)

    result = runner.invoke(app, ["files"])

    assert result.exit_code == 0
    assert "present" in result.output
    assert "/media/movie.mkv" in result.output


def test_files_command_sorts_by_path(tmp_path: Path) -> None:
    config_path = _init_config(tmp_path)
    _insert_media_file(config_path, "/media/b.mkv", MediaFileStatus.PRESENT)
    _insert_media_file(config_path, "/media/a.mkv", MediaFileStatus.PRESENT)

    result = runner.invoke(app, ["files"])

    assert result.exit_code == 0
    assert result.output.index("/media/a.mkv") < result.output.index("/media/b.mkv")


def test_files_changed_includes_added_files(tmp_path: Path) -> None:
    config_path = _init_config(tmp_path)
    _insert_media_file(config_path, "/media/added.mkv", MediaFileStatus.ADDED)

    result = runner.invoke(app, ["files", "--changed"])

    assert "added" in result.output


def test_files_changed_includes_changed_files(tmp_path: Path) -> None:
    config_path = _init_config(tmp_path)
    _insert_media_file(config_path, "/media/changed.mkv", MediaFileStatus.CHANGED)

    result = runner.invoke(app, ["files", "--changed"])

    assert "changed" in result.output


def test_files_changed_includes_missing_files(tmp_path: Path) -> None:
    config_path = _init_config(tmp_path)
    _insert_media_file(config_path, "/media/missing.mkv", MediaFileStatus.MISSING)

    result = runner.invoke(app, ["files", "--changed"])

    assert "missing" in result.output


def test_files_changed_excludes_present_files(tmp_path: Path) -> None:
    config_path = _init_config(tmp_path)
    _insert_media_file(config_path, "/media/present.mkv", MediaFileStatus.PRESENT)

    result = runner.invoke(app, ["files", "--changed"])

    assert "/media/present.mkv" not in result.output


def test_files_command_handles_empty_inventory(tmp_path: Path) -> None:
    _init_config(tmp_path)

    result = runner.invoke(app, ["files"])

    assert result.exit_code == 0
    assert "STATUS" in result.output


def test_files_command_requires_workspace(
    tmp_path: Path,
) -> None:
    result = runner.invoke(app, ["files"])

    assert result.exit_code != 0
    assert "No Avarch workspace found" in result.output


def _init_config(tmp_path: Path) -> Path:
    config_path = tmp_path / ".avarch" / "config.toml"
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    return config_path


def _insert_media_file(config_path: Path, path: str, status: MediaFileStatus) -> None:
    app_config = load_config(config_path)
    engine = create_db_engine(resolve_database_url(app_config, config_path))
    now = datetime(2026, 6, 14, tzinfo=UTC)
    with Session(engine) as session:
        session.add(
            MediaFile(
                path=path,
                size_bytes=1024,
                mtime_ns=123,
                device_id=456,
                inode=789,
                fs_fingerprint=f"key:{path}",
                discovered_at=now,
                last_seen_at=now,
                status=status,
            )
        )
        session.commit()
