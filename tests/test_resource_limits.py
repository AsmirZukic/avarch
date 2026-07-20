from __future__ import annotations

import pytest

from avarch.application.resource_limits import (
    ResourceLimitError,
    parse_memory_reserve,
    resolve_resource_limits,
)
from avarch.application.resources import ResourceConfidence, ResourceSnapshot, ResourceValue


def test_parse_memory_reserve_accepts_absolute_units() -> None:
    reserve = parse_memory_reserve("2GiB")

    assert reserve.kind == "bytes"
    assert reserve.value == 2 * 1024**3


def test_parse_memory_reserve_accepts_percent() -> None:
    reserve = parse_memory_reserve("12.5%")

    assert reserve.kind == "percent"
    assert reserve.value == 0.125


def test_parse_memory_reserve_rejects_impossible_values() -> None:
    for value in ("", "-1", "100%", "ten"):
        with pytest.raises(ResourceLimitError):
            parse_memory_reserve(value)


def test_resolve_resource_limits_separates_hard_limit_reserve_and_usable_budget() -> None:
    limits = resolve_resource_limits(
        snapshot=_snapshot(cpu_count=8, memory_bytes=16 * 1024**3),
        cpu_reserve=1.5,
        memory_reserve=parse_memory_reserve("25%"),
    )

    assert limits.hard_cpu_count == 8
    assert limits.cpu_reserve == 1.5
    assert limits.usable_cpu_count == 6.5
    assert limits.hard_memory_bytes == 16 * 1024**3
    assert limits.memory_reserve_bytes == 4 * 1024**3
    assert limits.usable_memory_bytes == 12 * 1024**3


def test_resolve_resource_limits_rejects_reserve_exceeding_detected_allocation() -> None:
    with pytest.raises(ResourceLimitError, match="CPU reserve"):
        resolve_resource_limits(
            snapshot=_snapshot(cpu_count=2, memory_bytes=8 * 1024**3),
            cpu_reserve=2,
            memory_reserve=parse_memory_reserve("10%"),
        )

    with pytest.raises(ResourceLimitError, match="memory reserve"):
        resolve_resource_limits(
            snapshot=_snapshot(cpu_count=2, memory_bytes=8 * 1024**3),
            cpu_reserve=1,
            memory_reserve=parse_memory_reserve("8GiB"),
        )


def _snapshot(*, cpu_count: int, memory_bytes: int) -> ResourceSnapshot:
    return ResourceSnapshot(
        effective_cpu_count=cpu_count,
        effective_cpu_quota=float(cpu_count),
        effective_memory_bytes=memory_bytes,
        cpu_values=(ResourceValue("cgroup_v2", float(cpu_count), ResourceConfidence.HIGH),),
        memory_values=(ResourceValue("cgroup_v2", memory_bytes, ResourceConfidence.HIGH),),
    )
