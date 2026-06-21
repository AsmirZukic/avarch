from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import Engine
from sqlmodel import Session, col, select
from typer.testing import CliRunner

from avarch.cli import app
from avarch.config import load_config, resolve_database_url
from avarch.db import create_db_engine
from avarch.models.db import Job, MediaFile
from avarch.models.scheduler import JobStage, JobStatus
from avarch.workspace import (
    WorkspaceContext,
    WorkspaceCreateError,
    WorkspaceError,
    WorkspacePathError,
    create_workspace,
)

runner = CliRunner()


def test_workspace_derives_all_paths(tmp_path: Path) -> None:
    workspace = WorkspaceContext(tmp_path)

    assert workspace.avarch_dir == tmp_path / ".avarch"
    assert workspace.workspace_toml == tmp_path / ".avarch" / "workspace.toml"
    assert workspace.config_toml == tmp_path / ".avarch" / "config.toml"
    assert workspace.profiles_dir == tmp_path / ".avarch" / "profiles"
    assert workspace.scripts_dir == tmp_path / ".avarch" / "scripts"
    assert workspace.vpy_requirements_toml == tmp_path / ".avarch" / "vpy" / "requirements.toml"
    assert workspace.database_path == tmp_path / ".avarch" / "data" / "avarch.db"
    assert workspace.work_dir == tmp_path / ".avarch" / "work"


def test_workspace_path_cannot_escape_root(tmp_path: Path) -> None:
    workspace = WorkspaceContext(tmp_path)

    with pytest.raises(WorkspacePathError):
        workspace.resolve_inside(Path("..") / "outside.mkv")


def test_create_workspace_reports_permission_denied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    denied_path = tmp_path / ".avarch.tmp-denied"

    def fail_mkdtemp(*_args: object, **_kwargs: object) -> str:
        raise PermissionError(13, "Permission denied", str(denied_path))

    monkeypatch.setattr("avarch.workspace.tempfile.mkdtemp", fail_mkdtemp)

    with pytest.raises(WorkspaceCreateError, match="Cannot initialize Avarch workspace"):
        create_workspace(tmp_path)


def test_init_reports_workspace_errors_without_traceback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PWD", str(tmp_path))

    def fail_create_workspace(*_args: object, **_kwargs: object) -> WorkspaceContext:
        raise WorkspaceError("Cannot initialize Avarch workspace at /workspace")

    monkeypatch.setattr("avarch.cli.create_workspace", fail_create_workspace)

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 1
    assert "Cannot initialize Avarch workspace" in result.output
    assert "Traceback" not in result.output


def test_init_creates_complete_workspace_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PWD", str(tmp_path))

    result = runner.invoke(app, ["init"])

    assert result.exit_code == 0
    assert (tmp_path / ".avarch" / "workspace.toml").is_file()
    assert (tmp_path / ".avarch" / "config.toml").is_file()
    assert (tmp_path / ".avarch" / "profiles").is_dir()
    assert (tmp_path / ".avarch" / "scripts").is_dir()
    assert (tmp_path / ".avarch" / "vpy" / "requirements.toml").is_file()
    assert (tmp_path / ".avarch" / "vpy" / "environments").is_dir()
    assert (tmp_path / ".avarch" / "data" / "avarch.db").is_file()
    assert (tmp_path / ".avarch" / "logs").is_dir()
    assert (tmp_path / ".avarch" / "work").is_dir()
    assert (tmp_path / ".avarch" / "tmp").is_dir()
    assert (tmp_path / ".avarch" / "run").is_dir()


def test_workspace_scan_persists_relative_media_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PWD", str(tmp_path))
    init_result = runner.invoke(app, ["init"])
    media_dir = tmp_path / "Movies"
    media_dir.mkdir()
    movie = media_dir / "Test.mkv"
    movie.write_bytes(b"media")

    scan_result = runner.invoke(app, ["scan", "."])

    assert init_result.exit_code == 0
    assert scan_result.exit_code == 0
    assert _media_paths(tmp_path) == ["Movies/Test.mkv"]


def test_same_job_id_can_exist_in_two_workspaces(tmp_path: Path) -> None:
    workspace_a = tmp_path / "a"
    workspace_b = tmp_path / "b"
    workspace_a.mkdir()
    workspace_b.mkdir()

    _init_scan_and_enqueue(workspace_a)
    _init_scan_and_enqueue(workspace_b)

    assert _job_ids(workspace_a) == [1]
    assert _job_ids(workspace_b) == [1]


def test_workspace_a_never_reads_workspace_b_jobs(tmp_path: Path) -> None:
    workspace_a = tmp_path / "a"
    workspace_b = tmp_path / "b"
    workspace_a.mkdir()
    workspace_b.mkdir()

    _init_scan_and_enqueue(workspace_a, movie_name="a.mkv")
    _init_scan_and_enqueue(workspace_b, movie_name="b.mkv")

    assert _job_media_paths(workspace_a) == ["Media/a.mkv"]
    assert _job_media_paths(workspace_b) == ["Media/b.mkv"]


def test_create_workspace_does_not_overwrite_existing_workspace(tmp_path: Path) -> None:
    create_workspace(tmp_path)

    with pytest.raises(Exception, match="Workspace already exists"):
        create_workspace(tmp_path)


def _media_paths(workspace_root: Path) -> list[str]:
    with Session(_engine(workspace_root)) as session:
        return [media_file.path for media_file in session.exec(select(MediaFile)).all()]


def _job_ids(workspace_root: Path) -> list[int]:
    with Session(_engine(workspace_root)) as session:
        return [job.id or 0 for job in session.exec(select(Job).order_by(col(Job.id))).all()]


def _job_media_paths(workspace_root: Path) -> list[str]:
    with Session(_engine(workspace_root)) as session:
        jobs = session.exec(select(Job).order_by(col(Job.id))).all()
        paths: list[str] = []
        for job in jobs:
            media_file = session.get(MediaFile, job.media_file_id)
            if media_file is not None:
                paths.append(media_file.path)
        return paths


def _engine(workspace_root: Path) -> Engine:
    config_path = workspace_root / ".avarch" / "config.toml"
    app_config = load_config(config_path)
    return create_db_engine(resolve_database_url(app_config, config_path))


def _init_scan_and_enqueue(workspace_root: Path, *, movie_name: str = "movie.mkv") -> None:
    init_result = runner.invoke(app, ["init"], env={"PWD": str(workspace_root)})
    media_dir = workspace_root / "Media"
    media_dir.mkdir()
    (media_dir / movie_name).write_bytes(b"media")
    scan_result = runner.invoke(app, ["scan", "."], env={"PWD": str(workspace_root)})
    now = datetime.now(UTC)
    with Session(_engine(workspace_root)) as session, session.begin():
        media_file = session.exec(select(MediaFile)).one()
        session.add(
            Job(
                media_file_id=media_file.id or 0,
                profile_name="default",
                profile_hash="profile",
                source_fs_fingerprint=media_file.fs_fingerprint,
                queue_key=f"queue:{movie_name}",
                status=JobStatus.PENDING,
                stage=JobStage.ENCODE,
                priority=0,
                attempts=0,
                created_at=now,
                updated_at=now,
            )
        )

    assert init_result.exit_code == 0
    assert scan_result.exit_code == 0
