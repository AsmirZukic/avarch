from pathlib import Path

import pytest
from typer.testing import CliRunner

from avarch.application.resources import (
    CgroupCpuQuota,
    CgroupCpuset,
    CgroupMemoryLimit,
    ResourceSnapshot,
    ResourceStatus,
)
from avarch.cli import app

runner = CliRunner()


def test_cli_help_exits_successfully() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "avarch" in result.output.lower()


def test_cli_version_exits_successfully() -> None:
    result = runner.invoke(app, ["--version"])

    assert result.exit_code == 0
    assert "0.2.0" in result.output


def test_cli_version_command_exits_successfully() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert "0.2.0" in result.output


@pytest.mark.parametrize(
    "command",
    [
        ["init"],
        ["doctor"],
        ["scan"],
        ["files", "list"],
        ["probe"],
        ["files", "show"],
        ["plan"],
        ["promote"],
        ["workflow"],
        ["workflow", "run"],
        ["system"],
        ["system", "resources"],
        ["db"],
        ["db", "current"],
        ["db", "upgrade"],
    ],
)
def test_user_can_check_help_for_each_command(command: list[str]) -> None:
    result = runner.invoke(app, [*command, "--help"])

    assert result.exit_code == 0
    assert "Usage:" in result.output


def test_system_resources_reports_stable_detected_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    snapshot = ResourceSnapshot(
        host_logical_cpu_count=16,
        affinity_cpu_count=8,
        cgroup_cpuset=CgroupCpuset(
            status=ResourceStatus.LIMITED,
            version="v2",
            path="/sys/fs/cgroup/cpuset.cpus.effective",
            value="0-7",
            cpu_count=8,
        ),
        cgroup_cpu_quota=CgroupCpuQuota(
            status=ResourceStatus.LIMITED,
            version="v2",
            path="/sys/fs/cgroup/cpu.max",
            quota_us=550000,
            period_us=100000,
            capacity=5.5,
        ),
        effective_cpu_count=5,
        effective_cpu_sources=("cgroup v2 CPU quota",),
        host_memory_bytes=16 * 1024**3,
        cgroup_memory=CgroupMemoryLimit(
            status=ResourceStatus.LIMITED,
            version="v2",
            path="/sys/fs/cgroup/memory.max",
            limit_bytes=6 * 1024**3,
        ),
        effective_memory_bytes=6 * 1024**3,
        effective_memory_sources=("cgroup v2 memory limit",),
    )
    monkeypatch.setattr("avarch.cli.effective_resource_snapshot", lambda: snapshot)

    result = runner.invoke(app, ["system", "resources"])

    assert result.exit_code == 0
    assert "host logical:     16" in result.output
    assert "process affinity: 8" in result.output
    assert "cgroup cpuset:    0-7 (8 CPUs; cgroup v2 cpuset.cpus.effective)" in result.output
    assert "550000 / 100000 us = 5.50 CPUs (cgroup v2 cpu.max)" in result.output
    assert "effective:        5 (cgroup v2 CPU quota)" in result.output
    assert "quota rounding:   floor to whole CPUs; minimum 1" in result.output
    assert "host physical:    16.0 GiB (17179869184 bytes)" in result.output
    assert "cgroup limit:     6.0 GiB (6442450944 bytes)" in result.output


def test_system_resources_operates_outside_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["system", "resources"])

    assert result.exit_code == 0
    assert "Resources" in result.output
    assert not (tmp_path / ".avarch").exists()
