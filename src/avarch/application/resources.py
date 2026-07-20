from __future__ import annotations

import math
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

SVT_AV1_AUTO_CPUS_PER_WORKER = 3
SVT_AV1_AUTO_MAX_WORKERS = 4
SVT_AV1_AUTO_BYTES_PER_WORKER = 3 * 1024 * 1024 * 1024


class ResourceConfidence(StrEnum):
    HIGH = "high"
    LOW = "low"
    DEGRADED = "degraded"


@dataclass(frozen=True, slots=True)
class ResourceValue:
    source: str
    value: int | float | None
    confidence: ResourceConfidence
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    effective_cpu_count: int | None
    effective_cpu_quota: float | None
    effective_memory_bytes: int | None
    cpu_values: tuple[ResourceValue, ...]
    memory_values: tuple[ResourceValue, ...]

    @property
    def degraded(self) -> bool:
        values = (*self.cpu_values, *self.memory_values)
        return not values or any(value.confidence != ResourceConfidence.HIGH for value in values)


def available_cpu_count() -> int:
    snapshot = effective_resource_snapshot()
    if snapshot.effective_cpu_count is not None:
        return max(1, snapshot.effective_cpu_count)
    return 1


def available_memory_bytes() -> int | None:
    return effective_resource_snapshot().effective_memory_bytes


def effective_resource_snapshot(
    *,
    sys_fs_cgroup: Path = Path("/sys/fs/cgroup"),
) -> ResourceSnapshot:
    cpu_values = _cpu_resource_values(sys_fs_cgroup=sys_fs_cgroup)
    memory_values = _memory_resource_values(sys_fs_cgroup=sys_fs_cgroup)
    cpu_quota = next(
        (
            value.value
            for value in cpu_values
            if value.source in {"cgroup_v2", "cgroup_v1"} and isinstance(value.value, float)
        ),
        None,
    )
    cpu_candidates = [
        value.value for value in cpu_values if isinstance(value.value, int) and value.value > 0
    ]
    if isinstance(cpu_quota, float) and cpu_quota > 0:
        cpu_candidates.append(max(1, math.floor(cpu_quota)))
    memory_candidates = [
        value.value for value in memory_values if isinstance(value.value, int) and value.value > 0
    ]
    return ResourceSnapshot(
        effective_cpu_count=min(cpu_candidates) if cpu_candidates else None,
        effective_cpu_quota=cpu_quota,
        effective_memory_bytes=min(memory_candidates) if memory_candidates else None,
        cpu_values=tuple(cpu_values),
        memory_values=tuple(memory_values),
    )


def _cpu_resource_values(*, sys_fs_cgroup: Path) -> list[ResourceValue]:
    candidates: list[int] = []
    values: list[ResourceValue] = []
    cpuset_count = _cgroup_cpuset_count(sys_fs_cgroup=sys_fs_cgroup)
    if cpuset_count is not None:
        candidates.append(cpuset_count)
        values.append(ResourceValue("cpuset", cpuset_count, ResourceConfidence.HIGH))
    affinity_count = _cpu_affinity_count()
    if affinity_count is not None:
        candidates.append(affinity_count)
        values.append(ResourceValue("affinity", affinity_count, ResourceConfidence.HIGH))
    quota = _cgroup_cpu_quota(sys_fs_cgroup=sys_fs_cgroup)
    if quota is not None:
        quota_count = _cgroup_cpu_quota_count(sys_fs_cgroup=sys_fs_cgroup) or max(
            1,
            math.floor(quota),
        )
        candidates.append(quota_count)
        source = "cgroup_v2" if (sys_fs_cgroup / "cpu.max").exists() else "cgroup_v1"
        values.append(ResourceValue(source, quota, ResourceConfidence.HIGH))
    os_count = os.cpu_count()
    if os_count is not None:
        candidates.append(os_count)
        values.append(ResourceValue("host_fallback", os_count, ResourceConfidence.LOW))
    if not values:
        values.append(ResourceValue("unknown", None, ResourceConfidence.DEGRADED, "unavailable"))
    return values


def _memory_resource_values(*, sys_fs_cgroup: Path) -> list[ResourceValue]:
    values: list[ResourceValue] = []
    cgroup_limit = _cgroup_memory_limit_bytes(sys_fs_cgroup=sys_fs_cgroup)
    if cgroup_limit is not None:
        source = "cgroup_v2" if (sys_fs_cgroup / "memory.max").exists() else "cgroup_v1"
        values.append(ResourceValue(source, cgroup_limit, ResourceConfidence.HIGH))
    physical_memory = _physical_memory_bytes()
    if physical_memory is not None:
        values.append(ResourceValue("host_fallback", physical_memory, ResourceConfidence.LOW))
    if not values:
        values.append(ResourceValue("unknown", None, ResourceConfidence.DEGRADED, "unavailable"))
    return values


def auto_av1an_worker_count(
    *,
    cpu_count: int | None = None,
    memory_bytes: int | None = None,
    reserved_cpus: int = 1,
    cpus_per_worker: int = SVT_AV1_AUTO_CPUS_PER_WORKER,
    max_workers: int = SVT_AV1_AUTO_MAX_WORKERS,
    bytes_per_worker: int = SVT_AV1_AUTO_BYTES_PER_WORKER,
) -> int:
    available = available_cpu_count() if cpu_count is None else cpu_count
    memory = available_memory_bytes() if memory_bytes is None else memory_bytes

    cpu_budget = max(1, available - reserved_cpus)
    cpu_bound = max(1, math.floor(cpu_budget / cpus_per_worker))
    worker_count = min(max_workers, cpu_bound)
    if memory is not None:
        memory_bound = max(1, math.floor(memory / bytes_per_worker))
        worker_count = min(worker_count, memory_bound)
    return max(1, worker_count)


def _cpu_affinity_count() -> int | None:
    try:
        affinity = os.sched_getaffinity(0)
    except (AttributeError, OSError):
        return None
    return len(affinity) or None


def _cgroup_cpu_quota_count(*, sys_fs_cgroup: Path = Path("/sys/fs/cgroup")) -> int | None:
    quota = _cgroup_cpu_quota(sys_fs_cgroup=sys_fs_cgroup)
    if quota is None:
        return None
    return max(1, math.floor(quota))


def _cgroup_cpu_quota(*, sys_fs_cgroup: Path = Path("/sys/fs/cgroup")) -> float | None:
    v2_quota = _cgroup_v2_cpu_quota(sys_fs_cgroup / "cpu.max")
    if v2_quota is not None:
        return v2_quota
    return _cgroup_v1_cpu_quota(
        sys_fs_cgroup / "cpu" / "cpu.cfs_quota_us",
        sys_fs_cgroup / "cpu" / "cpu.cfs_period_us",
    )


def _cgroup_v2_cpu_quota(path: Path) -> float | None:
    try:
        quota_text, period_text, *_ = path.read_text(encoding="utf-8").split()
    except (OSError, ValueError):
        return None
    if quota_text == "max":
        return None
    return _quota_to_cpu_quota(quota_text, period_text)


def _cgroup_v1_cpu_quota(quota_path: Path, period_path: Path) -> float | None:
    try:
        quota_text = quota_path.read_text(encoding="utf-8").strip()
        period_text = period_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return _quota_to_cpu_quota(quota_text, period_text)


def _quota_to_cpu_quota(quota_text: str, period_text: str) -> float | None:
    try:
        quota = int(quota_text)
        period = int(period_text)
    except ValueError:
        return None
    if quota <= 0 or period <= 0:
        return None
    return quota / period


def _cgroup_cpuset_count(*, sys_fs_cgroup: Path = Path("/sys/fs/cgroup")) -> int | None:
    for path in (
        sys_fs_cgroup / "cpuset.cpus.effective",
        sys_fs_cgroup / "cpuset.cpus",
    ):
        count = _read_cpuset_count(path)
        if count is not None:
            return count
    return None


def _read_cpuset_count(path: Path) -> int | None:
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    cpus: set[int] = set()
    for part in value.split(","):
        if not part:
            continue
        if "-" in part:
            start_text, end_text = part.split("-", maxsplit=1)
            try:
                start = int(start_text)
                end = int(end_text)
            except ValueError:
                return None
            if start > end:
                return None
            cpus.update(range(start, end + 1))
            continue
        try:
            cpus.add(int(part))
        except ValueError:
            return None
    return len(cpus) or None


def _cgroup_memory_limit_bytes(*, sys_fs_cgroup: Path = Path("/sys/fs/cgroup")) -> int | None:
    v2_limit = _read_memory_limit(sys_fs_cgroup / "memory.max")
    if v2_limit is not None:
        return v2_limit
    return _read_memory_limit(sys_fs_cgroup / "memory" / "memory.limit_in_bytes")


def _read_memory_limit(path: Path) -> int | None:
    try:
        limit_text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if limit_text == "max":
        return None
    try:
        limit = int(limit_text)
    except ValueError:
        return None
    if limit <= 0 or limit >= 1 << 60:
        return None
    return limit


def _physical_memory_bytes() -> int | None:
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        physical_pages = os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, OSError, ValueError):
        return None
    if page_size <= 0 or physical_pages <= 0:
        return None
    return page_size * physical_pages
