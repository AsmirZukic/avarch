from pathlib import Path

import pytest

from avarch.application.resources import (
    _cgroup_cpu_quota_count,  # pyright: ignore[reportPrivateUsage]
    _cgroup_memory_limit_bytes,  # pyright: ignore[reportPrivateUsage]
    effective_resource_snapshot,
)


def test_cgroup_v2_cpu_quota_limits_available_count(tmp_path: Path) -> None:
    (tmp_path / "cpu.max").write_text("550000 100000\n", encoding="utf-8")

    assert _cgroup_cpu_quota_count(sys_fs_cgroup=tmp_path) == 5


def test_cgroup_v2_unlimited_quota_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "cpu.max").write_text("max 100000\n", encoding="utf-8")

    assert _cgroup_cpu_quota_count(sys_fs_cgroup=tmp_path) is None


def test_cgroup_v1_cpu_quota_limits_available_count(tmp_path: Path) -> None:
    cpu_dir = tmp_path / "cpu"
    cpu_dir.mkdir()
    (cpu_dir / "cpu.cfs_quota_us").write_text("250000\n", encoding="utf-8")
    (cpu_dir / "cpu.cfs_period_us").write_text("100000\n", encoding="utf-8")

    assert _cgroup_cpu_quota_count(sys_fs_cgroup=tmp_path) == 2


def test_cgroup_v2_memory_limit_is_detected(tmp_path: Path) -> None:
    (tmp_path / "memory.max").write_text(f"{6 * 1024**3}\n", encoding="utf-8")

    assert _cgroup_memory_limit_bytes(sys_fs_cgroup=tmp_path) == 6 * 1024**3


def test_cgroup_v2_unlimited_memory_is_ignored(tmp_path: Path) -> None:
    (tmp_path / "memory.max").write_text("max\n", encoding="utf-8")

    assert _cgroup_memory_limit_bytes(sys_fs_cgroup=tmp_path) is None


def test_resource_snapshot_uses_most_restrictive_cpu_allocation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "cpu.max").write_text("550000 100000\n", encoding="utf-8")
    (tmp_path / "cpuset.cpus.effective").write_text("0-7\n", encoding="utf-8")
    monkeypatch.setattr("avarch.application.resources._cpu_affinity_count", lambda: 6)
    monkeypatch.setattr("avarch.application.resources.os.cpu_count", lambda: 16)
    monkeypatch.setattr("avarch.application.resources._physical_memory_bytes", lambda: None)

    snapshot = effective_resource_snapshot(sys_fs_cgroup=tmp_path)

    assert snapshot.effective_cpu_count == 5
    assert snapshot.effective_cpu_quota == 5.5
    assert {value.source for value in snapshot.cpu_values} >= {
        "cgroup_v2",
        "cpuset",
        "affinity",
        "host_fallback",
    }


def test_resource_snapshot_reads_cgroup_v1_memory_limit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "memory.limit_in_bytes").write_text(f"{3 * 1024**3}\n", encoding="utf-8")
    monkeypatch.setattr("avarch.application.resources._cpu_affinity_count", lambda: None)
    monkeypatch.setattr("avarch.application.resources.os.cpu_count", lambda: None)
    monkeypatch.setattr("avarch.application.resources._physical_memory_bytes", lambda: 8 * 1024**3)

    snapshot = effective_resource_snapshot(sys_fs_cgroup=tmp_path)

    assert snapshot.effective_memory_bytes == 3 * 1024**3
    assert [value.source for value in snapshot.memory_values] == ["cgroup_v1", "host_fallback"]


def test_resource_snapshot_unlimited_values_do_not_become_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "cpu.max").write_text("max 100000\n", encoding="utf-8")
    (tmp_path / "memory.max").write_text("9223372036854771712\n", encoding="utf-8")
    monkeypatch.setattr("avarch.application.resources._cpu_affinity_count", lambda: None)
    monkeypatch.setattr("avarch.application.resources.os.cpu_count", lambda: None)
    monkeypatch.setattr("avarch.application.resources._physical_memory_bytes", lambda: None)

    snapshot = effective_resource_snapshot(sys_fs_cgroup=tmp_path)

    assert snapshot.effective_cpu_count is None
    assert snapshot.effective_memory_bytes is None
    assert snapshot.degraded is True


def test_resource_snapshot_reports_degraded_unknowns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("avarch.application.resources._cpu_affinity_count", lambda: None)
    monkeypatch.setattr("avarch.application.resources.os.cpu_count", lambda: None)
    monkeypatch.setattr("avarch.application.resources._physical_memory_bytes", lambda: None)

    snapshot = effective_resource_snapshot(sys_fs_cgroup=tmp_path)

    assert snapshot.effective_cpu_count is None
    assert snapshot.effective_memory_bytes is None
    sources = {value.source for value in (*snapshot.cpu_values, *snapshot.memory_values)}
    assert sources == {"unknown"}
