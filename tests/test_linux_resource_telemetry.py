from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from avarch.adapters.system.resource_telemetry import LinuxResourceSampler
from avarch.application.resource_telemetry import ResourceMetric, ResourceSample


def test_cgroup_v2_memory_is_used_when_constrained(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    cgroup = tmp_path / "cgroup"
    proc.mkdir()
    cgroup.mkdir()
    (cgroup / "memory.current").write_text("1024\n", encoding="utf-8")
    (cgroup / "memory.max").write_text("4096\n", encoding="utf-8")

    sample = LinuxResourceSampler(proc_root=proc, cgroup_root=cgroup).sample()
    memory = _metric(sample, "memory")

    assert memory.value == 1024
    assert memory.total == 4096


def test_unlimited_cgroup_memory_falls_back_to_host_meminfo(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    cgroup = tmp_path / "cgroup"
    proc.mkdir()
    cgroup.mkdir()
    (cgroup / "memory.current").write_text("1024\n", encoding="utf-8")
    (cgroup / "memory.max").write_text("max\n", encoding="utf-8")
    (proc / "meminfo").write_text(
        "MemTotal:       1000 kB\nMemAvailable:    250 kB\n",
        encoding="utf-8",
    )

    sample = LinuxResourceSampler(proc_root=proc, cgroup_root=cgroup).sample()
    memory = _metric(sample, "memory")

    assert memory.value == 750 * 1024
    assert memory.total == 1000 * 1024


def test_cgroup_cpu_percentage_uses_injected_clock(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    cgroup = tmp_path / "cgroup"
    proc.mkdir()
    cgroup.mkdir()
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    times = iter([now, now + timedelta(seconds=2)])
    sampler = LinuxResourceSampler(proc_root=proc, cgroup_root=cgroup, clock=lambda: next(times))

    (cgroup / "cpu.stat").write_text("usage_usec 1000000\n", encoding="utf-8")
    first = sampler.sample()
    (cgroup / "cpu.stat").write_text("usage_usec 2000000\n", encoding="utf-8")
    second = sampler.sample()

    assert _metric(first, "cpu").available is False
    assert _metric(second, "cpu").value == 50.0


def test_host_proc_stat_cpu_percentage_fallback(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    cgroup = tmp_path / "cgroup"
    proc.mkdir()
    cgroup.mkdir()
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    times = iter([now, now + timedelta(seconds=1)])
    sampler = LinuxResourceSampler(proc_root=proc, cgroup_root=cgroup, clock=lambda: next(times))

    (proc / "stat").write_text("cpu  10 0 0 90\n", encoding="utf-8")
    sampler.sample()
    (proc / "stat").write_text("cpu  20 0 0 180\n", encoding="utf-8")
    second = sampler.sample()

    assert _metric(second, "cpu").value == 10.0


def test_malformed_or_missing_files_return_unavailable_metrics(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    cgroup = tmp_path / "cgroup"
    proc.mkdir()
    cgroup.mkdir()
    (cgroup / "memory.current").write_text("not-int\n", encoding="utf-8")
    (cgroup / "memory.max").write_text("4096\n", encoding="utf-8")

    sample = LinuxResourceSampler(proc_root=proc, cgroup_root=cgroup).sample()

    assert _metric(sample, "cpu").available is False
    assert _metric(sample, "memory").available is False


def test_cpu_counter_reset_returns_unavailable_delta(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    cgroup = tmp_path / "cgroup"
    proc.mkdir()
    cgroup.mkdir()
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    times = iter([now, now + timedelta(seconds=1)])
    sampler = LinuxResourceSampler(proc_root=proc, cgroup_root=cgroup, clock=lambda: next(times))

    (cgroup / "cpu.stat").write_text("usage_usec 2000000\n", encoding="utf-8")
    sampler.sample()
    (cgroup / "cpu.stat").write_text("usage_usec 1000000\n", encoding="utf-8")
    second = sampler.sample()

    assert _metric(second, "cpu").available is False
    assert _metric(second, "cpu").reason == "delta_unavailable"


def _metric(sample: ResourceSample, name: str) -> ResourceMetric:
    return next(metric for metric in sample.metrics if metric.name == name)
