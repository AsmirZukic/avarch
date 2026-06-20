from pathlib import Path

from sqlmodel import Session, select
from typer.testing import CliRunner

from avarch.cli import app
from avarch.config import load_config, resolve_database_url
from avarch.db import create_db_engine
from avarch.models.db import MediaFile, MediaFileStatus

runner = CliRunner()


def test_scanner_inventory_lifecycle_smoke(tmp_path: Path) -> None:
    config_path = _init_config(tmp_path)
    library = tmp_path / "library"
    library.mkdir()
    movie_a = library / "movie-a.mkv"
    movie_b = library / "movie-b.mp4"
    movie_a.write_bytes(b"a")
    movie_b.write_bytes(b"bb")
    (library / "notes.txt").write_text("ignored", encoding="utf-8")
    work_dir = library / ".avarch"
    work_dir.mkdir()
    (work_dir / "temporary.mkv").write_bytes(b"ignored")

    initial = runner.invoke(app, ["scan", str(library)])

    assert initial.exit_code == 0
    assert "Added:     2" in initial.output
    assert "Changed:   0" in initial.output
    assert "Missing:   0" in initial.output
    rows = _media_files(config_path)
    assert len(rows) == 2
    assert {row.status for row in rows} == {MediaFileStatus.ADDED}

    second = runner.invoke(app, ["scan", str(library)])

    assert second.exit_code == 0
    assert "Added:     0" in second.output
    assert "Unchanged: 2" in second.output
    rows = _media_files(config_path)
    assert len(rows) == 2
    assert {row.status for row in rows} == {MediaFileStatus.PRESENT}
    changed_files = runner.invoke(app, ["files", "--changed"])
    assert "movie-a.mkv" not in changed_files.output
    assert "movie-b.mp4" not in changed_files.output

    movie_a.write_bytes(b"aaa")
    modified = runner.invoke(app, ["scan", str(library)])

    assert modified.exit_code == 0
    assert "Changed:   1" in modified.output
    statuses = {Path(row.path).name: row.status for row in _media_files(config_path)}
    assert statuses == {
        "movie-a.mkv": MediaFileStatus.CHANGED,
        "movie-b.mp4": MediaFileStatus.PRESENT,
    }

    movie_b.unlink()
    removed = runner.invoke(app, ["scan", str(library)])

    assert removed.exit_code == 0
    assert "Missing:   1" in removed.output
    rows = _media_files(config_path)
    assert len(rows) == 2
    statuses = {Path(row.path).name: row.status for row in rows}
    assert statuses["movie-b.mp4"] == MediaFileStatus.MISSING

    movie_b.write_bytes(b"restored")
    restored = runner.invoke(app, ["scan", str(library)])

    assert restored.exit_code == 0
    assert "Changed:   1" in restored.output
    rows = _media_files(config_path)
    assert len(rows) == 2
    statuses = {Path(row.path).name: row.status for row in rows}
    assert statuses["movie-b.mp4"] == MediaFileStatus.CHANGED


def _init_config(tmp_path: Path) -> Path:
    config_path = tmp_path / ".avarch" / "config.toml"
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    return config_path


def _media_files(config_path: Path) -> list[MediaFile]:
    app_config = load_config(config_path)
    engine = create_db_engine(resolve_database_url(app_config, config_path))
    with Session(engine) as session:
        return list(session.exec(select(MediaFile).order_by(MediaFile.path)).all())
