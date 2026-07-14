from pathlib import Path

from avarch.application.resources import (
    _cgroup_cpu_quota_count,  # pyright: ignore[reportPrivateUsage]
    auto_av1an_worker_count,
)


def test_auto_av1an_worker_count_reserves_one_cpu() -> None:
    assert auto_av1an_worker_count(cpu_count=16) == 15


def test_auto_av1an_worker_count_keeps_single_cpu_usable() -> None:
    assert auto_av1an_worker_count(cpu_count=1) == 1


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
