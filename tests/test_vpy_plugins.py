from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from avarch.adapters.vpy_plugins import VpyPluginInfo, plugin_info_from_object
from avarch.cli import app

runner = CliRunner()


def test_plugin_info_classifies_workspace_plugin() -> None:
    plugin = SimpleNamespace(
        namespace="fmtc",
        name="fmtconv",
        version="1.0",
        path="/workspace/.avarch/vpy/environments/env/plugins/libfmtconv.so",
    )

    info = plugin_info_from_object(plugin)

    assert info.namespace == "fmtc"
    assert info.name == "fmtconv"
    assert info.version == "1.0"
    assert info.source == "workspace"
    assert info.path == Path(plugin.path)


def test_vpy_plugins_command_lists_inventory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "avarch.bootstrap.list_vapoursynth_plugins",
        lambda: (
            VpyPluginInfo(
                namespace="bs",
                name="BestSource",
                version="18",
                source="runtime",
                path=Path("/opt/libbestsource.so"),
            ),
        ),
    )

    result = runner.invoke(app, ["vpy", "plugins"])

    assert result.exit_code == 0
    assert "NAMESPACE" in result.output
    assert "BestSource" in result.output
    assert "/opt/libbestsource.so" in result.output


def test_vpy_env_check_reports_plugin_namespaces(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PWD", str(tmp_path))
    monkeypatch.setattr(
        "avarch.bootstrap.list_vapoursynth_plugins",
        lambda: (
            VpyPluginInfo(
                namespace="bs",
                name="BestSource",
                version="18",
                source="runtime",
                path=Path("/opt/libbestsource.so"),
            ),
        ),
    )

    init_result = runner.invoke(app, ["init"])
    result = runner.invoke(app, ["vpy", "env", "check"])

    assert init_result.exit_code == 0
    assert result.exit_code == 0
    assert "Plugin namespaces: bs" in result.output
