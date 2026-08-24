from pathlib import Path

from avarch.application.resources import (
    CGROUP_V1_UNLIMITED_MEMORY_THRESHOLD,
    ResourceSnapshot,
    ResourceStatus,
    _parse_cpuset_count,  # pyright: ignore[reportPrivateUsage]
    auto_av1an_worker_count,
    effective_resource_snapshot,
)


def test_auto_av1an_worker_count_caps_svt_parallelism() -> None:
    assert auto_av1an_worker_count(cpu_count=20, memory_bytes=64 * 1024**3) == 4


def test_auto_av1an_worker_count_scales_down_for_small_cpu_budget() -> None:
    assert auto_av1an_worker_count(cpu_count=8, memory_bytes=64 * 1024**3) == 2


def test_auto_av1an_worker_count_scales_down_for_small_memory_budget() -> None:
    assert auto_av1an_worker_count(cpu_count=20, memory_bytes=4 * 1024**3) == 1


def test_auto_av1an_worker_count_keeps_single_cpu_usable() -> None:
    assert auto_av1an_worker_count(cpu_count=1, memory_bytes=64 * 1024**3) == 1


def test_no_cgroup_limits_uses_host_resources(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, host_cpus=12, affinity_cpus=None, host_memory=8 * 1024**3)

    assert snapshot.effective_cpu_count == 12
    assert snapshot.effective_cpu_sources == ("host logical CPUs",)
    assert snapshot.cgroup_cpuset.status == ResourceStatus.UNAVAILABLE
    assert snapshot.cgroup_cpu_quota.status == ResourceStatus.UNAVAILABLE
    assert snapshot.effective_memory_bytes == 8 * 1024**3
    assert snapshot.effective_memory_sources == ("host physical memory",)
    assert snapshot.cgroup_memory.status == ResourceStatus.UNAVAILABLE


def test_cpu_affinity_limits_effective_count(tmp_path: Path) -> None:
    snapshot = _snapshot(tmp_path, host_cpus=16, affinity_cpus=6)

    assert snapshot.effective_cpu_count == 6
    assert snapshot.effective_cpu_sources == ("process affinity",)


def test_cgroup_cpuset_limits_effective_count(tmp_path: Path) -> None:
    (tmp_path / "cpuset.cpus.effective").write_text("0-2,8\n", encoding="utf-8")

    snapshot = _snapshot(tmp_path, host_cpus=16, affinity_cpus=None)

    assert snapshot.cgroup_cpuset.value == "0-2,8"
    assert snapshot.cgroup_cpuset.cpu_count == 4
    assert snapshot.cgroup_cpuset.version == "v2"
    assert snapshot.effective_cpu_count == 4
    assert snapshot.effective_cpu_sources == ("cgroup v2 cpuset",)


def test_cgroup_v1_cpuset_layout_is_supported(tmp_path: Path) -> None:
    cpuset_dir = tmp_path / "cpuset"
    cpuset_dir.mkdir()
    (cpuset_dir / "cpuset.effective_cpus").write_text("1,3-4\n", encoding="utf-8")

    snapshot = _snapshot(tmp_path, host_cpus=8, affinity_cpus=None)

    assert snapshot.cgroup_cpuset.version == "v1"
    assert snapshot.cgroup_cpuset.cpu_count == 3
    assert snapshot.effective_cpu_count == 3


def test_cgroup_v2_cpu_quota_reports_exact_and_rounded_capacity(tmp_path: Path) -> None:
    (tmp_path / "cpu.max").write_text("550000 100000\n", encoding="utf-8")

    snapshot = _snapshot(tmp_path, host_cpus=16, affinity_cpus=None)

    assert snapshot.cgroup_cpu_quota.quota_us == 550000
    assert snapshot.cgroup_cpu_quota.period_us == 100000
    assert snapshot.cgroup_cpu_quota.capacity == 5.5
    assert snapshot.effective_cpu_count == 5


def test_fractional_cpu_quota_keeps_one_usable_cpu(tmp_path: Path) -> None:
    (tmp_path / "cpu.max").write_text("50000 100000\n", encoding="utf-8")

    snapshot = _snapshot(tmp_path, host_cpus=8, affinity_cpus=4)

    assert snapshot.cgroup_cpu_quota.capacity == 0.5
    assert snapshot.effective_cpu_count == 1
    assert snapshot.effective_cpu_sources == ("cgroup v2 CPU quota",)


def test_most_restrictive_cpu_limit_wins(tmp_path: Path) -> None:
    (tmp_path / "cpuset.cpus.effective").write_text("0-7\n", encoding="utf-8")
    (tmp_path / "cpu.max").write_text("550000 100000\n", encoding="utf-8")

    snapshot = _snapshot(tmp_path, host_cpus=16, affinity_cpus=6)

    assert snapshot.effective_cpu_count == 5
    assert snapshot.effective_cpu_sources == ("cgroup v2 CPU quota",)


def test_cpuset_parser_supports_ranges_duplicates_and_rejects_empty_or_malformed() -> None:
    assert _parse_cpuset_count("0-3,8,10-11") == 7
    assert _parse_cpuset_count("0-2,2-4") == 5
    assert _parse_cpuset_count("") is None
    assert _parse_cpuset_count("3-1") is None
    assert _parse_cpuset_count("0,,2") is None
    assert _parse_cpuset_count("0-") is None
    assert _parse_cpuset_count("cpu0") is None


def test_malformed_cgroup_cpu_values_are_reported_and_ignored(tmp_path: Path) -> None:
    (tmp_path / "cpu.max").write_text("not-a-quota\n", encoding="utf-8")
    (tmp_path / "cpuset.cpus.effective").write_text("4-2\n", encoding="utf-8")

    snapshot = _snapshot(tmp_path, host_cpus=8, affinity_cpus=None)

    assert snapshot.cgroup_cpu_quota.status == ResourceStatus.INVALID
    assert snapshot.cgroup_cpuset.status == ResourceStatus.INVALID
    assert snapshot.effective_cpu_count == 8


def test_cgroup_v2_unlimited_quota_is_reported_and_ignored(tmp_path: Path) -> None:
    (tmp_path / "cpu.max").write_text("max 100000\n", encoding="utf-8")

    snapshot = _snapshot(tmp_path, host_cpus=8, affinity_cpus=None)

    assert snapshot.cgroup_cpu_quota.status == ResourceStatus.UNLIMITED
    assert snapshot.effective_cpu_count == 8


def test_cgroup_v1_cpu_quota_layout_is_supported(tmp_path: Path) -> None:
    cpu_dir = tmp_path / "cpu"
    cpu_dir.mkdir()
    (cpu_dir / "cpu.cfs_quota_us").write_text("250000\n", encoding="utf-8")
    (cpu_dir / "cpu.cfs_period_us").write_text("100000\n", encoding="utf-8")

    snapshot = _snapshot(tmp_path, host_cpus=8, affinity_cpus=None)

    assert snapshot.cgroup_cpu_quota.version == "v1"
    assert snapshot.cgroup_cpu_quota.capacity == 2.5
    assert snapshot.effective_cpu_count == 2


def test_cgroup_v1_combined_cpu_controller_layout_is_supported(tmp_path: Path) -> None:
    cpu_dir = tmp_path / "cpu,cpuacct"
    cpu_dir.mkdir()
    (cpu_dir / "cpu.cfs_quota_us").write_text("300000\n", encoding="utf-8")
    (cpu_dir / "cpu.cfs_period_us").write_text("100000\n", encoding="utf-8")

    snapshot = _snapshot(tmp_path, host_cpus=8, affinity_cpus=None)

    assert snapshot.cgroup_cpu_quota.version == "v1"
    assert snapshot.cgroup_cpu_quota.capacity == 3.0
    assert snapshot.effective_cpu_count == 3


def test_cgroup_v2_memory_limit_is_detected(tmp_path: Path) -> None:
    (tmp_path / "memory.max").write_text(f"{6 * 1024**3}\n", encoding="utf-8")

    snapshot = _snapshot(tmp_path, host_memory=16 * 1024**3)

    assert snapshot.cgroup_memory.status == ResourceStatus.LIMITED
    assert snapshot.cgroup_memory.version == "v2"
    assert snapshot.effective_memory_bytes == 6 * 1024**3
    assert snapshot.effective_memory_sources == ("cgroup v2 memory limit",)


def test_cgroup_v2_unlimited_memory_uses_host_memory(tmp_path: Path) -> None:
    (tmp_path / "memory.max").write_text("max\n", encoding="utf-8")

    snapshot = _snapshot(tmp_path, host_memory=16 * 1024**3)

    assert snapshot.cgroup_memory.status == ResourceStatus.UNLIMITED
    assert snapshot.cgroup_memory.limit_bytes is None
    assert snapshot.effective_memory_bytes == 16 * 1024**3


def test_cgroup_v1_unlimited_memory_sentinel_is_ignored(tmp_path: Path) -> None:
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir()
    (memory_dir / "memory.limit_in_bytes").write_text(
        f"{CGROUP_V1_UNLIMITED_MEMORY_THRESHOLD}\n",
        encoding="utf-8",
    )

    snapshot = _snapshot(tmp_path, host_memory=4 * 1024**3)

    assert snapshot.cgroup_memory.status == ResourceStatus.UNLIMITED
    assert snapshot.effective_memory_bytes == 4 * 1024**3


def test_malformed_memory_limit_is_reported_and_ignored(tmp_path: Path) -> None:
    (tmp_path / "memory.max").write_text("many\n", encoding="utf-8")

    snapshot = _snapshot(tmp_path, host_memory=4 * 1024**3)

    assert snapshot.cgroup_memory.status == ResourceStatus.INVALID
    assert snapshot.effective_memory_bytes == 4 * 1024**3


def test_host_information_unavailable_uses_safe_fallbacks(tmp_path: Path) -> None:
    snapshot = _snapshot(
        tmp_path,
        host_cpus=None,
        affinity_cpus=None,
        host_memory=None,
    )

    assert snapshot.host_logical_cpu_count is None
    assert snapshot.effective_cpu_count == 1
    assert snapshot.effective_cpu_sources == ("minimum fallback",)
    assert snapshot.host_memory_bytes is None
    assert snapshot.effective_memory_bytes is None
    assert snapshot.effective_memory_sources == ()


def test_non_linux_fallback_does_not_read_cgroup_files(tmp_path: Path) -> None:
    (tmp_path / "cpu.max").write_text("100000 100000\n", encoding="utf-8")
    (tmp_path / "memory.max").write_text("1024\n", encoding="utf-8")

    snapshot = _snapshot(
        tmp_path,
        platform_name="Darwin",
        host_cpus=10,
        affinity_cpus=None,
        host_memory=8 * 1024**3,
    )

    assert snapshot.effective_cpu_count == 10
    assert snapshot.cgroup_cpu_quota.status == ResourceStatus.UNAVAILABLE
    assert snapshot.cgroup_memory.status == ResourceStatus.UNAVAILABLE
    assert snapshot.effective_memory_bytes == 8 * 1024**3


def test_injectable_reader_supports_virtual_cgroup_files(tmp_path: Path) -> None:
    values = {
        tmp_path / "cpu.max": "300000 100000\n",
        tmp_path / "memory.max": str(2 * 1024**3),
    }

    def read_text(path: Path) -> str:
        try:
            return values[path]
        except KeyError as exc:
            raise FileNotFoundError(path) from exc

    snapshot = effective_resource_snapshot(
        sys_fs_cgroup=tmp_path,
        platform_name="Linux",
        read_text=read_text,
        host_cpu_reader=lambda: 8,
        affinity_reader=lambda: None,
        host_memory_reader=lambda: 4 * 1024**3,
    )

    assert snapshot.effective_cpu_count == 3
    assert snapshot.effective_memory_bytes == 2 * 1024**3


def _snapshot(
    sys_fs_cgroup: Path,
    *,
    platform_name: str = "Linux",
    host_cpus: int | None = 16,
    affinity_cpus: int | None = 16,
    host_memory: int | None = 8 * 1024**3,
) -> ResourceSnapshot:
    return effective_resource_snapshot(
        sys_fs_cgroup=sys_fs_cgroup,
        platform_name=platform_name,
        host_cpu_reader=lambda: host_cpus,
        affinity_reader=lambda: affinity_cpus,
        host_memory_reader=lambda: host_memory,
    )
