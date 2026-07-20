from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from avarch.application.resources import ResourceSnapshot

type MemoryReserveKind = Literal["bytes", "percent"]


class ResourceLimitError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class MemoryReserve:
    kind: MemoryReserveKind
    value: int | float


@dataclass(frozen=True, slots=True)
class ResolvedResourceLimits:
    hard_cpu_count: int | None
    hard_memory_bytes: int | None
    cpu_reserve: float
    usable_cpu_count: float | None
    memory_reserve_bytes: int | None
    usable_memory_bytes: int | None


def parse_memory_reserve(value: int | str) -> MemoryReserve:
    if isinstance(value, int):
        if value < 0:
            raise ResourceLimitError("memory reserve must not be negative")
        return MemoryReserve(kind="bytes", value=value)

    text = value.strip().lower()
    if not text:
        raise ResourceLimitError("memory reserve must not be empty")
    if text.endswith("%"):
        return _parse_percent_reserve(text[:-1])
    return MemoryReserve(kind="bytes", value=_parse_byte_count(text))


def resolve_resource_limits(
    *,
    snapshot: ResourceSnapshot,
    cpu_reserve: int | float,
    memory_reserve: MemoryReserve,
) -> ResolvedResourceLimits:
    if cpu_reserve < 0:
        raise ResourceLimitError("CPU reserve must not be negative")
    hard_cpu = snapshot.effective_cpu_count
    usable_cpu = None
    if hard_cpu is not None:
        if cpu_reserve >= hard_cpu:
            raise ResourceLimitError("CPU reserve must be smaller than detected CPU allocation")
        usable_cpu = hard_cpu - float(cpu_reserve)

    hard_memory = snapshot.effective_memory_bytes
    reserve_bytes = _resolve_memory_reserve_bytes(memory_reserve, total_bytes=hard_memory)
    usable_memory = None
    if hard_memory is not None and reserve_bytes is not None:
        if reserve_bytes >= hard_memory:
            raise ResourceLimitError(
                "memory reserve must be smaller than detected memory allocation"
            )
        usable_memory = hard_memory - reserve_bytes

    return ResolvedResourceLimits(
        hard_cpu_count=hard_cpu,
        hard_memory_bytes=hard_memory,
        cpu_reserve=float(cpu_reserve),
        usable_cpu_count=usable_cpu,
        memory_reserve_bytes=reserve_bytes,
        usable_memory_bytes=usable_memory,
    )


def _parse_percent_reserve(text: str) -> MemoryReserve:
    try:
        percent = float(text)
    except ValueError as exc:
        raise ResourceLimitError("memory reserve percent must be numeric") from exc
    if percent < 0 or percent >= 100:
        raise ResourceLimitError("memory reserve percent must be in [0, 100)")
    return MemoryReserve(kind="percent", value=percent / 100.0)


def _parse_byte_count(text: str) -> int:
    match = re.fullmatch(r"([0-9]+)(?:\s*([kmgt]i?b?|b))?", text)
    if match is None:
        raise ResourceLimitError("memory reserve must be bytes or a percentage")
    amount = int(match.group(1))
    unit = match.group(2) or "b"
    multiplier = {
        "b": 1,
        "k": 1000,
        "kb": 1000,
        "ki": 1024,
        "kib": 1024,
        "m": 1000**2,
        "mb": 1000**2,
        "mi": 1024**2,
        "mib": 1024**2,
        "g": 1000**3,
        "gb": 1000**3,
        "gi": 1024**3,
        "gib": 1024**3,
        "t": 1000**4,
        "tb": 1000**4,
        "ti": 1024**4,
        "tib": 1024**4,
    }[unit]
    return amount * multiplier


def _resolve_memory_reserve_bytes(
    reserve: MemoryReserve,
    *,
    total_bytes: int | None,
) -> int | None:
    if reserve.kind == "bytes":
        return int(reserve.value)
    if total_bytes is None:
        return None
    return int(total_bytes * float(reserve.value))
