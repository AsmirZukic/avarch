from __future__ import annotations

import math
import os
from pathlib import Path

SVT_AV1_AUTO_CPUS_PER_WORKER = 3
SVT_AV1_AUTO_MAX_WORKERS = 4
SVT_AV1_AUTO_BYTES_PER_WORKER = 3 * 1024 * 1024 * 1024


def available_cpu_count() -> int:
    candidates: list[int] = []
    affinity_count = _cpu_affinity_count()
    if affinity_count is not None:
        candidates.append(affinity_count)
    quota_count = _cgroup_cpu_quota_count()
    if quota_count is not None:
        candidates.append(quota_count)
    os_count = os.cpu_count()
    if os_count is not None:
        candidates.append(os_count)
    return max(1, min(candidates)) if candidates else 1


def available_memory_bytes() -> int | None:
    candidates: list[int] = []
    cgroup_limit = _cgroup_memory_limit_bytes()
    if cgroup_limit is not None:
        candidates.append(cgroup_limit)
    physical_memory = _physical_memory_bytes()
    if physical_memory is not None:
        candidates.append(physical_memory)
    return min(candidates) if candidates else None


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
    v2_count = _cgroup_v2_cpu_quota_count(sys_fs_cgroup / "cpu.max")
    if v2_count is not None:
        return v2_count
    return _cgroup_v1_cpu_quota_count(
        sys_fs_cgroup / "cpu" / "cpu.cfs_quota_us",
        sys_fs_cgroup / "cpu" / "cpu.cfs_period_us",
    )


def _cgroup_v2_cpu_quota_count(path: Path) -> int | None:
    try:
        quota_text, period_text, *_ = path.read_text(encoding="utf-8").split()
    except (OSError, ValueError):
        return None
    if quota_text == "max":
        return None
    return _quota_to_cpu_count(quota_text, period_text)


def _cgroup_v1_cpu_quota_count(quota_path: Path, period_path: Path) -> int | None:
    try:
        quota_text = quota_path.read_text(encoding="utf-8").strip()
        period_text = period_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return _quota_to_cpu_count(quota_text, period_text)


def _quota_to_cpu_count(quota_text: str, period_text: str) -> int | None:
    try:
        quota = int(quota_text)
        period = int(period_text)
    except ValueError:
        return None
    if quota <= 0 or period <= 0:
        return None
    return max(1, math.floor(quota / period))


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
