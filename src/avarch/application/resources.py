from __future__ import annotations

import math
import os
from pathlib import Path


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


def auto_av1an_worker_count(*, cpu_count: int | None = None, reserved_cpus: int = 1) -> int:
    available = available_cpu_count() if cpu_count is None else cpu_count
    return max(1, available - reserved_cpus)


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
