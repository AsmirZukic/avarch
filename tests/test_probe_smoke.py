from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlmodel import Session, select
from typer.testing import CliRunner

from avarch.cli import app
from avarch.config import load_config, resolve_database_url
from avarch.db import create_db_engine
from avarch.models.db import MediaFile, MediaFileStatus, ProbeResult
from tests.probe_fixtures import representative_probe_payload

runner = CliRunner()


def test_probe_smoke_workflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "avarch.toml"
    library = tmp_path / "library"
    library.mkdir()
    media_file = library / "movie.mkv"
    media_file.write_bytes(b"media")

    init_result = runner.invoke(app, ["init", "--config", str(config_path)])
    assert init_result.exit_code == 0

    scan_result = runner.invoke(app, ["scan", str(library), "--config", str(config_path)])
    assert scan_result.exit_code == 0

    before = _media_file(config_path, media_file)
    assert before.status == MediaFileStatus.ADDED

    payloads: Iterator[subprocess.CompletedProcess[str]] = iter(
        [
            subprocess.CompletedProcess(
                ["ffprobe"],
                0,
                stdout=json.dumps(representative_probe_payload()),
                stderr="",
            ),
            subprocess.CompletedProcess(
                ["ffprobe"],
                0,
                stdout=json.dumps(representative_probe_payload(duration="601.000000")),
                stderr="",
            ),
            subprocess.CompletedProcess(["ffprobe"], 1, stdout="", stderr="broken"),
        ]
    )

    def fake_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return next(payloads)

    monkeypatch.setattr(subprocess, "run", fake_run)

    probe_result = runner.invoke(app, ["probe", str(media_file), "--config", str(config_path)])
    assert probe_result.exit_code == 0
    assert "Container: matroska,webm" in probe_result.output
    assert _probe_result_count(config_path) == 1
    first_probe = _latest_probe_result(config_path)
    assert '"format"' in first_probe.ffprobe_json
    assert '"container":"matroska,webm"' in first_probe.normalized_json
    assert first_probe.probe_hash in probe_result.output

    after = _media_file(config_path, media_file)
    assert after.status == before.status
    assert after.content_key == before.content_key
    assert after.last_seen_at == before.last_seen_at

    inspect_result = runner.invoke(app, ["inspect", str(media_file), "--config", str(config_path)])
    assert inspect_result.exit_code == 0
    assert _summary(inspect_result.output) == _summary(probe_result.output)

    second_probe = runner.invoke(app, ["probe", str(media_file), "--config", str(config_path)])
    assert second_probe.exit_code == 0
    assert "Duration: 601 s" in second_probe.output
    assert _probe_result_count(config_path) == 2

    second_inspect = runner.invoke(app, ["inspect", str(media_file), "--config", str(config_path)])
    assert second_inspect.exit_code == 0
    assert _summary(second_inspect.output) == _summary(second_probe.output)

    failed_probe = runner.invoke(app, ["probe", str(media_file), "--config", str(config_path)])
    assert failed_probe.exit_code != 0
    assert "broken" in failed_probe.output
    assert _probe_result_count(config_path) == 2


def _media_file(config_path: Path, path: Path) -> MediaFile:
    engine = _engine(config_path)
    with Session(engine) as session:
        return session.exec(select(MediaFile).where(MediaFile.path == str(path.resolve()))).one()


def _probe_result_count(config_path: Path) -> int:
    engine = _engine(config_path)
    with Session(engine) as session:
        return len(session.exec(select(ProbeResult)).all())


def _latest_probe_result(config_path: Path) -> ProbeResult:
    engine = _engine(config_path)
    with Session(engine) as session:
        results = session.exec(select(ProbeResult)).all()
    assert results
    return max(results, key=lambda result: result.id or 0)


def _summary(output: str) -> str:
    return output[output.index("File: ") :]


def _engine(config_path: Path) -> Engine:
    app_config = load_config(config_path)
    return create_db_engine(resolve_database_url(app_config, config_path))
