from pathlib import Path

from sqlmodel import Session, select
from typer.testing import CliRunner

from avarch.cli import app
from avarch.config import load_config, resolve_database_url
from avarch.db import create_db_engine
from avarch.models.db import MediaFile

runner = CliRunner()


def test_scan_command_scans_explicit_root(tmp_path: Path) -> None:
    _init_config(tmp_path)
    media_root = _media_root(tmp_path)

    result = runner.invoke(app, ["scan", str(media_root)])

    assert result.exit_code == 0
    assert "Scan complete." in result.output


def test_scan_command_uses_configured_roots(tmp_path: Path) -> None:
    config_path = _init_config(tmp_path)
    media_root = _media_root(tmp_path)
    _write_configured_root(config_path, media_root)

    result = runner.invoke(app, ["scan"])

    assert result.exit_code == 0
    assert str(media_root) in result.output


def test_scan_command_fails_without_roots(tmp_path: Path) -> None:
    _init_config(tmp_path)

    result = runner.invoke(app, ["scan"])

    assert result.exit_code != 0
    assert "No scan roots" in result.output


def test_scan_command_persists_inventory(tmp_path: Path) -> None:
    config_path = _init_config(tmp_path)
    media_root = _media_root(tmp_path)

    result = runner.invoke(app, ["scan", str(media_root)])

    assert result.exit_code == 0
    assert _media_file_count(config_path) == 1


def test_scan_command_reports_counts(tmp_path: Path) -> None:
    _init_config(tmp_path)
    media_root = _media_root(tmp_path)

    result = runner.invoke(app, ["scan", str(media_root)])

    assert result.exit_code == 0
    assert "Added:     1" in result.output
    assert "Changed:   0" in result.output
    assert "Missing:   0" in result.output


def test_scan_command_fails_for_invalid_root(tmp_path: Path) -> None:
    _init_config(tmp_path)

    result = runner.invoke(app, ["scan", str(tmp_path / "missing")])

    assert result.exit_code != 0
    assert "Root does not exist" in result.output


def test_scan_command_requires_workspace(
    tmp_path: Path,
) -> None:
    media_root = _media_root(tmp_path)

    result = runner.invoke(app, ["scan", str(media_root)])

    assert result.exit_code != 0
    assert "No Avarch workspace found" in result.output


def _init_config(tmp_path: Path) -> Path:
    config_path = tmp_path / ".avarch" / "config.toml"
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    return config_path


def _media_root(tmp_path: Path) -> Path:
    media_root = tmp_path / "media"
    media_root.mkdir()
    (media_root / "movie.mkv").write_bytes(b"abc")
    return media_root


def _write_configured_root(config_path: Path, media_root: Path) -> None:
    config_text = config_path.read_text(encoding="utf-8")
    config_text = config_text.replace("roots = []", f'roots = ["{media_root}"]')
    config_path.write_text(config_text, encoding="utf-8")


def _media_file_count(config_path: Path) -> int:
    app_config = load_config(config_path)
    engine = create_db_engine(resolve_database_url(app_config, config_path))
    with Session(engine) as session:
        return len(session.exec(select(MediaFile)).all())
