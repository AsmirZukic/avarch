from pathlib import Path

from avarch.application.resources import (
    _cgroup_cpu_quota_count,  # pyright: ignore[reportPrivateUsage]
    _cgroup_memory_limit_bytes,  # pyright: ignore[reportPrivateUsage]
    auto_av1an_worker_count,
)


def test_auto_av1an_worker_count_caps_svt_parallelism() -> None:
    assert auto_av1an_worker_count(cpu_count=20, memory_bytes=64 * 1024**3) == 4


def test_auto_av1an_worker_count_scales_down_for_small_cpu_budget() -> None:
    assert auto_av1an_worker_count(cpu_count=8, memory_bytes=64 * 1024**3) == 2


def test_auto_av1an_worker_count_scales_down_for_small_memory_budget() -> None:
    assert auto_av1an_worker_count(cpu_count=20, memory_bytes=4 * 1024**3) == 1


def test_auto_av1an_worker_count_keeps_single_cpu_usable() -> None:
    assert auto_av1an_worker_count(cpu_count=1, memory_bytes=64 * 1024**3) == 1


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
