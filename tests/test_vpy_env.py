from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from avarch.cli import app
from avarch.vpy_env import (
    VpyRequirements,
    add_python_package,
    add_vsrepo_package,
    build_runtime_identity,
    load_requirements,
    remove_python_package,
    remove_vsrepo_package,
    runtime_environment_variables,
    sync_environment,
)
from avarch.workspace import WorkspaceContext, create_workspace

runner = CliRunner()


def test_requirements_manifest_is_strict(tmp_path: Path) -> None:
    workspace = create_workspace(tmp_path)
    workspace.vpy_requirements_toml.write_text(
        """
schema_version = 1
unexpected = true

[python]
packages = []
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValidationError):
        load_requirements(workspace)


def test_environment_identity_changes_with_manifest() -> None:
    first = build_runtime_identity(VpyRequirements()).environment_id
    second = build_runtime_identity(
        VpyRequirements.model_validate({"python": {"packages": ["example==1"]}})
    ).environment_id

    assert first != second


def test_sync_environment_writes_lock_and_current(tmp_path: Path) -> None:
    workspace = create_workspace(tmp_path)

    environment = sync_environment(workspace)

    assert environment.lock_path.is_file()
    assert workspace.vpy_current.read_text(encoding="utf-8").strip() == (
        environment.identity.environment_id
    )
    assert (environment.path / "python").is_dir()
    assert (environment.path / "plugins").is_dir()
    assert "manifest_hash" in environment.lock_path.read_text(encoding="utf-8")


def test_package_manifest_edits_are_deduplicated(tmp_path: Path) -> None:
    workspace = create_workspace(tmp_path)

    add_python_package(workspace, "example==1")
    add_python_package(workspace, "example==1")
    add_vsrepo_package(workspace, "fmtconv")
    add_vsrepo_package(workspace, "fmtconv")
    remove_python_package(workspace, "missing==1")

    assert load_requirements(workspace).python.packages == ["example==1"]
    assert load_requirements(workspace).vsrepo.packages == ["fmtconv"]
    remove_python_package(workspace, "example==1")
    remove_vsrepo_package(workspace, "fmtconv")
    assert load_requirements(workspace).python.packages == []
    assert load_requirements(workspace).vsrepo.packages == []


def test_runtime_environment_variables_include_workspace_paths(tmp_path: Path) -> None:
    workspace = create_workspace(tmp_path)
    env = runtime_environment_variables(workspace)
    python_paths = env["PYTHONPATH"].split(":")

    assert str(workspace.scripts_dir) in python_paths
    assert str(sync_environment(workspace).path / "python") in python_paths
    assert env["VAPOURSYNTH_EXTRA_PLUGIN_PATH"].endswith("/plugins")
    assert env["LD_LIBRARY_PATH"].split(":")[0].endswith("/libraries")


def test_vpy_env_and_package_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PWD", str(tmp_path))
    init_result = runner.invoke(app, ["init"])
    show_result = runner.invoke(app, ["vpy", "env", "show"])
    check_result = runner.invoke(app, ["vpy", "env", "check"])
    list_result = runner.invoke(app, ["vpy", "packages", "list"])

    assert init_result.exit_code == 0
    assert show_result.exit_code == 0
    assert "Environment:" in show_result.output
    assert check_result.exit_code == 0
    assert "PASS vpy_environment" in check_result.output
    assert list_result.exit_code == 0
    assert "Python packages:" in list_result.output


def test_vpy_packages_install_and_remove_update_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PWD", str(tmp_path))
    init_result = runner.invoke(app, ["init"])

    def fake_sync(workspace: WorkspaceContext):
        identity = build_runtime_identity(load_requirements(workspace))
        return SimpleNamespace(
            identity=identity,
            lock_path=workspace.vpy_environments_dir / identity.environment_id / "lock.toml",
        )

    monkeypatch.setattr("avarch.cli.sync_environment", fake_sync)
    install_result = runner.invoke(app, ["vpy", "packages", "install", "demo==1"])
    remove_result = runner.invoke(app, ["vpy", "packages", "remove", "demo==1"])
    native_install_result = runner.invoke(
        app,
        ["vpy", "packages", "install", "--kind", "vsrepo", "fmtconv"],
    )
    native_remove_result = runner.invoke(
        app,
        ["vpy", "packages", "remove", "--kind", "vsrepo", "fmtconv"],
    )

    assert init_result.exit_code == 0
    assert install_result.exit_code == 0
    assert remove_result.exit_code == 0
    assert native_install_result.exit_code == 0
    assert native_remove_result.exit_code == 0
    assert load_requirements(WorkspaceContext(tmp_path)).python.packages == []
    assert load_requirements(WorkspaceContext(tmp_path)).vsrepo.packages == []
