from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from avarch.application.resource_telemetry import (
    ResourceMetric,
    ResourceSample,
    sample_health,
)


@dataclass(frozen=True, slots=True)
class _CpuCounters:
    used: int
    total: int | None = None
    observed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class _OutputFileState:
    size: int
    device_id: int
    inode: int


class LinuxResourceSampler:
    def __init__(
        self,
        *,
        proc_root: Path = Path("/proc"),
        cgroup_root: Path = Path("/sys/fs/cgroup"),
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._proc_root = proc_root
        self._cgroup_root = cgroup_root
        self._clock = clock or (lambda: datetime.now(UTC))
        self._previous_cpu: _CpuCounters | None = None

    def sample(self) -> ResourceSample:
        sampled_at = self._clock()
        metrics = (
            self._cpu_metric(sampled_at),
            self._memory_metric(),
        )
        return ResourceSample(
            sampled_at=sampled_at,
            metrics=metrics,
            health=sample_health(metrics),
        )

    def _cpu_metric(self, sampled_at: datetime) -> ResourceMetric:
        counters = _read_cgroup_cpu(self._cgroup_root)
        if counters is None:
            counters = _read_proc_cpu(self._proc_root)
        if counters is None:
            return ResourceMetric(name="cpu", available=False, reason="unavailable")
        counters = _CpuCounters(
            used=counters.used,
            total=counters.total,
            observed_at=sampled_at,
        )
        previous = self._previous_cpu
        self._previous_cpu = counters
        if previous is None or previous.observed_at is None:
            return ResourceMetric(name="cpu", available=False, reason="delta_unavailable")
        elapsed = (sampled_at - previous.observed_at).total_seconds()
        if elapsed <= 0 or counters.used < previous.used:
            return ResourceMetric(name="cpu", available=False, reason="delta_unavailable")
        if counters.total is not None and previous.total is not None:
            total_delta = counters.total - previous.total
            used_delta = counters.used - previous.used
            if total_delta <= 0 or used_delta < 0:
                return ResourceMetric(name="cpu", available=False, reason="delta_unavailable")
            percent = min(100.0, max(0.0, (used_delta / total_delta) * 100.0))
        else:
            percent = max(0.0, ((counters.used - previous.used) / 1_000_000.0 / elapsed) * 100.0)
        return ResourceMetric(name="cpu", value=percent, unit="percent")

    def _memory_metric(self) -> ResourceMetric:
        cgroup_memory = _read_cgroup_memory(self._cgroup_root)
        if cgroup_memory is not None:
            used, total = cgroup_memory
            return ResourceMetric(name="memory", value=used, total=total, unit="bytes")
        host_memory = _read_proc_memory(self._proc_root)
        if host_memory is not None:
            used, total = host_memory
            return ResourceMetric(name="memory", value=used, total=total, unit="bytes")
        return ResourceMetric(name="memory", available=False, reason="unavailable")


class OutputGrowthSampler:
    def __init__(
        self,
        *,
        paths: Callable[[], tuple[Path, ...]],
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._paths = paths
        self._clock = clock or (lambda: datetime.now(UTC))
        self._previous_sizes: dict[Path, _OutputFileState] = {}
        self._previous_observed_at: datetime | None = None

    def sample(self) -> ResourceSample:
        sampled_at = self._clock()
        current_sizes = _read_output_sizes(self._paths())
        previous_observed_at = self._previous_observed_at
        previous_sizes = self._previous_sizes
        self._previous_observed_at = sampled_at
        self._previous_sizes = current_sizes
        if previous_observed_at is None:
            metric = ResourceMetric(
                name="output write rate",
                available=False,
                reason="delta_unavailable",
            )
        else:
            elapsed = (sampled_at - previous_observed_at).total_seconds()
            if elapsed <= 0:
                metric = ResourceMetric(
                    name="output write rate",
                    available=False,
                    reason="delta_unavailable",
                )
            else:
                grown_bytes = _grown_output_bytes(
                    previous=previous_sizes,
                    current=current_sizes,
                )
                metric = ResourceMetric(
                    name="output write rate",
                    value=grown_bytes / elapsed,
                    unit="bytes_per_second",
                )
        return ResourceSample(
            sampled_at=sampled_at,
            metrics=(metric,),
            health=sample_health((metric,)),
        )


def _read_cgroup_cpu(cgroup_root: Path) -> _CpuCounters | None:
    try:
        fields = dict(
            line.split(maxsplit=1)
            for line in (cgroup_root / "cpu.stat").read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
        usage_usec = int(fields["usage_usec"])
    except (OSError, ValueError, KeyError):
        return None
    return _CpuCounters(used=usage_usec)


def _read_proc_cpu(proc_root: Path) -> _CpuCounters | None:
    try:
        first = (proc_root / "stat").read_text(encoding="utf-8").splitlines()[0]
        parts = [int(value) for value in first.split()[1:]]
    except (OSError, ValueError, IndexError):
        return None
    if len(parts) < 4:
        return None
    idle = parts[3] + (parts[4] if len(parts) > 4 else 0)
    total = sum(parts)
    return _CpuCounters(used=total - idle, total=total)


def _read_cgroup_memory(cgroup_root: Path) -> tuple[int, int] | None:
    try:
        current = int((cgroup_root / "memory.current").read_text(encoding="utf-8").strip())
        max_text = (cgroup_root / "memory.max").read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None
    if max_text == "max":
        return None
    try:
        maximum = int(max_text)
    except ValueError:
        return None
    if maximum <= 0 or maximum >= 1 << 60:
        return None
    return current, maximum


def _read_proc_memory(proc_root: Path) -> tuple[int, int] | None:
    try:
        fields: dict[str, int] = {}
        for line in (proc_root / "meminfo").read_text(encoding="utf-8").splitlines():
            key, value, *_rest = line.split()
            fields[key.rstrip(":")] = int(value) * 1024
        total = fields["MemTotal"]
        available = fields.get("MemAvailable", fields.get("MemFree"))
    except (OSError, ValueError, KeyError):
        return None
    if available is None:
        return None
    return max(0, total - available), total


def _read_output_sizes(paths: tuple[Path, ...]) -> dict[Path, _OutputFileState]:
    sizes: dict[Path, _OutputFileState] = {}
    for path in paths:
        try:
            stat_result = path.stat()
        except OSError:
            continue
        if not path.is_file():
            continue
        sizes[path] = _OutputFileState(
            size=stat_result.st_size,
            device_id=stat_result.st_dev,
            inode=stat_result.st_ino,
        )
    return sizes


def _grown_output_bytes(
    *,
    previous: dict[Path, _OutputFileState],
    current: dict[Path, _OutputFileState],
) -> int:
    grown = 0
    for path, current_state in current.items():
        previous_state = previous.get(path)
        if previous_state is None:
            continue
        if (
            current_state.device_id != previous_state.device_id
            or current_state.inode != previous_state.inode
        ):
            continue
        grown += max(0, current_state.size - previous_state.size)
    return grown
