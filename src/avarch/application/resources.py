from __future__ import annotations

import math
import os
import platform
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

SVT_AV1_AUTO_CPUS_PER_WORKER = 3
SVT_AV1_AUTO_MAX_WORKERS = 4
SVT_AV1_AUTO_BYTES_PER_WORKER = 3 * 1024 * 1024 * 1024

CGROUP_V1_UNLIMITED_MEMORY_THRESHOLD = 1 << 60

type ReadText = Callable[[Path], str]
type OptionalIntReader = Callable[[], int | None]


class ResourceStatus(StrEnum):
    LIMITED = "limited"
    UNLIMITED = "unlimited"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class CgroupCpuQuota:
    status: ResourceStatus
    version: str | None = None
    path: str | None = None
    quota_us: int | None = None
    period_us: int | None = None
    capacity: float | None = None


@dataclass(frozen=True, slots=True)
class CgroupCpuset:
    status: ResourceStatus
    version: str | None = None
    path: str | None = None
    value: str | None = None
    cpu_count: int | None = None


@dataclass(frozen=True, slots=True)
class CgroupMemoryLimit:
    status: ResourceStatus
    version: str | None = None
    path: str | None = None
    limit_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    host_logical_cpu_count: int | None
    affinity_cpu_count: int | None
    cgroup_cpuset: CgroupCpuset
    cgroup_cpu_quota: CgroupCpuQuota
    effective_cpu_count: int
    effective_cpu_sources: tuple[str, ...]
    host_memory_bytes: int | None
    cgroup_memory: CgroupMemoryLimit
    effective_memory_bytes: int | None
    effective_memory_sources: tuple[str, ...]


def available_cpu_count() -> int:
    return effective_resource_snapshot().effective_cpu_count


def available_memory_bytes() -> int | None:
    return effective_resource_snapshot().effective_memory_bytes


def effective_resource_snapshot(
    *,
    sys_fs_cgroup: Path = Path("/sys/fs/cgroup"),
    platform_name: str | None = None,
    read_text: ReadText | None = None,
    host_cpu_reader: OptionalIntReader | None = None,
    affinity_reader: OptionalIntReader | None = None,
    host_memory_reader: OptionalIntReader | None = None,
) -> ResourceSnapshot:
    """Return the effective process resource envelope without changing runtime policy."""
    system = platform.system() if platform_name is None else platform_name
    reader = _read_text if read_text is None else read_text
    host_cpus = _positive_optional_int(
        os.cpu_count() if host_cpu_reader is None else host_cpu_reader()
    )
    affinity_cpus = _positive_optional_int(
        _cpu_affinity_count() if affinity_reader is None else affinity_reader()
    )
    host_memory = _positive_optional_int(
        _physical_memory_bytes() if host_memory_reader is None else host_memory_reader()
    )

    if system == "Linux":
        cpuset = _detect_cgroup_cpuset(sys_fs_cgroup=sys_fs_cgroup, read_text=reader)
        quota = _detect_cgroup_cpu_quota(sys_fs_cgroup=sys_fs_cgroup, read_text=reader)
        cgroup_memory = _detect_cgroup_memory_limit(
            sys_fs_cgroup=sys_fs_cgroup,
            read_text=reader,
        )
    else:
        cpuset = CgroupCpuset(status=ResourceStatus.UNAVAILABLE)
        quota = CgroupCpuQuota(status=ResourceStatus.UNAVAILABLE)
        cgroup_memory = CgroupMemoryLimit(status=ResourceStatus.UNAVAILABLE)

    cpu_candidates: list[tuple[str, int]] = []
    if host_cpus is not None:
        cpu_candidates.append(("host logical CPUs", host_cpus))
    if affinity_cpus is not None:
        cpu_candidates.append(("process affinity", affinity_cpus))
    if cpuset.cpu_count is not None:
        cpu_candidates.append((_cgroup_source(cpuset.version, "cpuset"), cpuset.cpu_count))
    if quota.capacity is not None:
        cpu_candidates.append(
            (_cgroup_source(quota.version, "CPU quota"), max(1, math.floor(quota.capacity)))
        )
    effective_cpus = max(1, min((value for _source, value in cpu_candidates), default=1))
    cpu_sources = tuple(source for source, value in cpu_candidates if value == effective_cpus) or (
        "minimum fallback",
    )

    memory_candidates: list[tuple[str, int]] = []
    if host_memory is not None:
        memory_candidates.append(("host physical memory", host_memory))
    if cgroup_memory.limit_bytes is not None:
        memory_candidates.append(
            (_cgroup_source(cgroup_memory.version, "memory limit"), cgroup_memory.limit_bytes)
        )
    effective_memory = min((value for _source, value in memory_candidates), default=None)
    memory_sources = (
        tuple(source for source, value in memory_candidates if value == effective_memory)
        if effective_memory is not None
        else ()
    )

    return ResourceSnapshot(
        host_logical_cpu_count=host_cpus,
        affinity_cpu_count=affinity_cpus,
        cgroup_cpuset=cpuset,
        cgroup_cpu_quota=quota,
        effective_cpu_count=effective_cpus,
        effective_cpu_sources=cpu_sources,
        host_memory_bytes=host_memory,
        cgroup_memory=cgroup_memory,
        effective_memory_bytes=effective_memory,
        effective_memory_sources=memory_sources,
    )


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


def _detect_cgroup_cpu_quota(
    *,
    sys_fs_cgroup: Path,
    read_text: ReadText,
) -> CgroupCpuQuota:
    v2_path = sys_fs_cgroup / "cpu.max"
    v2_text = _optional_text(v2_path, read_text=read_text)
    if v2_text is not None:
        return _parse_cgroup_v2_cpu_quota(v2_text, path=v2_path)

    for controller in ("cpu", "cpu,cpuacct", "cpuacct,cpu"):
        quota_path = sys_fs_cgroup / controller / "cpu.cfs_quota_us"
        period_path = sys_fs_cgroup / controller / "cpu.cfs_period_us"
        quota_text = _optional_text(quota_path, read_text=read_text)
        period_text = _optional_text(period_path, read_text=read_text)
        if quota_text is None and period_text is None:
            continue
        if quota_text is None or period_text is None:
            return CgroupCpuQuota(
                status=ResourceStatus.INVALID,
                version="v1",
                path=f"{quota_path}, {period_path}",
            )
        return _parse_cgroup_v1_cpu_quota(quota_text, period_text, path=quota_path)
    return CgroupCpuQuota(status=ResourceStatus.UNAVAILABLE)


def _parse_cgroup_v2_cpu_quota(value: str, *, path: Path) -> CgroupCpuQuota:
    parts = value.split()
    if len(parts) != 2:
        return CgroupCpuQuota(status=ResourceStatus.INVALID, version="v2", path=str(path))
    quota_text, period_text = parts
    if quota_text == "max":
        period = _parse_positive_int(period_text)
        return CgroupCpuQuota(
            status=ResourceStatus.UNLIMITED if period is not None else ResourceStatus.INVALID,
            version="v2",
            path=str(path),
            period_us=period,
        )
    return _parsed_cpu_quota(quota_text, period_text, version="v2", path=str(path))


def _parse_cgroup_v1_cpu_quota(
    quota_text: str,
    period_text: str,
    *,
    path: Path,
) -> CgroupCpuQuota:
    try:
        quota = int(quota_text.strip())
    except ValueError:
        return CgroupCpuQuota(status=ResourceStatus.INVALID, version="v1", path=str(path))
    period = _parse_positive_int(period_text)
    if quota == -1 and period is not None:
        return CgroupCpuQuota(
            status=ResourceStatus.UNLIMITED,
            version="v1",
            path=str(path),
            quota_us=quota,
            period_us=period,
        )
    return _parsed_cpu_quota(str(quota), period_text, version="v1", path=str(path))


def _parsed_cpu_quota(
    quota_text: str,
    period_text: str,
    *,
    version: str,
    path: str,
) -> CgroupCpuQuota:
    quota = _parse_positive_int(quota_text)
    period = _parse_positive_int(period_text)
    if quota is None or period is None:
        return CgroupCpuQuota(status=ResourceStatus.INVALID, version=version, path=path)
    return CgroupCpuQuota(
        status=ResourceStatus.LIMITED,
        version=version,
        path=path,
        quota_us=quota,
        period_us=period,
        capacity=quota / period,
    )


def _detect_cgroup_cpuset(
    *,
    sys_fs_cgroup: Path,
    read_text: ReadText,
) -> CgroupCpuset:
    paths = (
        ("v2", sys_fs_cgroup / "cpuset.cpus.effective"),
        ("v2", sys_fs_cgroup / "cpuset.cpus"),
        ("v1", sys_fs_cgroup / "cpuset" / "cpuset.effective_cpus"),
        ("v1", sys_fs_cgroup / "cpuset" / "cpuset.cpus"),
    )
    invalid: CgroupCpuset | None = None
    for version, path in paths:
        text = _optional_text(path, read_text=read_text)
        if text is None:
            continue
        value = text.strip()
        count = _parse_cpuset_count(value)
        if count is not None:
            return CgroupCpuset(
                status=ResourceStatus.LIMITED,
                version=version,
                path=str(path),
                value=value,
                cpu_count=count,
            )
        invalid = CgroupCpuset(
            status=ResourceStatus.UNAVAILABLE if not value else ResourceStatus.INVALID,
            version=version,
            path=str(path),
            value=value or None,
        )
    return invalid or CgroupCpuset(status=ResourceStatus.UNAVAILABLE)


def _parse_cpuset_count(value: str) -> int | None:
    if not value:
        return None
    ranges: list[tuple[int, int]] = []
    for part in value.split(","):
        if not part or part.strip() != part:
            return None
        if "-" in part:
            bounds = part.split("-")
            if len(bounds) != 2:
                return None
            start = _parse_nonnegative_int(bounds[0])
            end = _parse_nonnegative_int(bounds[1])
            if start is None or end is None or start > end:
                return None
            ranges.append((start, end))
        else:
            cpu = _parse_nonnegative_int(part)
            if cpu is None:
                return None
            ranges.append((cpu, cpu))
    ranges.sort()
    count = 0
    current_start, current_end = ranges[0]
    for start, end in ranges[1:]:
        if start <= current_end + 1:
            current_end = max(current_end, end)
            continue
        count += current_end - current_start + 1
        current_start, current_end = start, end
    return count + current_end - current_start + 1


def _detect_cgroup_memory_limit(
    *,
    sys_fs_cgroup: Path,
    read_text: ReadText,
) -> CgroupMemoryLimit:
    v2_path = sys_fs_cgroup / "memory.max"
    v2_text = _optional_text(v2_path, read_text=read_text)
    if v2_text is not None:
        return _parse_memory_limit(v2_text, version="v2", path=v2_path)

    v1_path = sys_fs_cgroup / "memory" / "memory.limit_in_bytes"
    v1_text = _optional_text(v1_path, read_text=read_text)
    if v1_text is not None:
        return _parse_memory_limit(v1_text, version="v1", path=v1_path)
    return CgroupMemoryLimit(status=ResourceStatus.UNAVAILABLE)


def _parse_memory_limit(value: str, *, version: str, path: Path) -> CgroupMemoryLimit:
    text = value.strip()
    if version == "v2" and text == "max":
        return CgroupMemoryLimit(
            status=ResourceStatus.UNLIMITED,
            version=version,
            path=str(path),
        )
    try:
        limit = int(text)
    except ValueError:
        return CgroupMemoryLimit(
            status=ResourceStatus.INVALID,
            version=version,
            path=str(path),
        )
    if limit <= 0:
        return CgroupMemoryLimit(
            status=ResourceStatus.INVALID,
            version=version,
            path=str(path),
        )
    if version == "v1" and limit >= CGROUP_V1_UNLIMITED_MEMORY_THRESHOLD:
        return CgroupMemoryLimit(
            status=ResourceStatus.UNLIMITED,
            version=version,
            path=str(path),
        )
    return CgroupMemoryLimit(
        status=ResourceStatus.LIMITED,
        version=version,
        path=str(path),
        limit_bytes=limit,
    )


def _physical_memory_bytes() -> int | None:
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        physical_pages = os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, OSError, ValueError):
        return None
    if page_size <= 0 or physical_pages <= 0:
        return None
    return page_size * physical_pages


def _optional_text(path: Path, *, read_text: ReadText) -> str | None:
    try:
        return read_text(path)
    except OSError:
        return None


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _parse_positive_int(value: str) -> int | None:
    try:
        parsed = int(value.strip())
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _parse_nonnegative_int(value: str) -> int | None:
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None


def _positive_optional_int(value: int | None) -> int | None:
    return value if value is not None and value > 0 else None


def _cgroup_source(version: str | None, kind: str) -> str:
    return f"cgroup {version} {kind}" if version is not None else f"cgroup {kind}"
