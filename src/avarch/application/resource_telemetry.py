from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol


class ResourceHealth(StrEnum):
    OK = "ok"
    WARNING = "warning"
    CRITICAL = "critical"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ResourceMetric:
    name: str
    value: float | int | None = None
    total: float | int | None = None
    unit: str | None = None
    available: bool = True
    reason: str | None = None
    health: ResourceHealth = ResourceHealth.OK


@dataclass(frozen=True, slots=True)
class ResourceSample:
    sampled_at: datetime
    metrics: tuple[ResourceMetric, ...]
    health: ResourceHealth = ResourceHealth.OK
    error: str | None = None

    @property
    def available(self) -> bool:
        return self.health != ResourceHealth.UNAVAILABLE


class ResourceSampler(Protocol):
    def sample(self) -> ResourceSample: ...


class NullResourceSampler:
    def __init__(
        self,
        *,
        reason: str = "unsupported",
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._reason = reason
        self._clock = clock or (lambda: datetime.now(UTC))

    def sample(self) -> ResourceSample:
        return unavailable_sample(sampled_at=self._clock(), reason=self._reason)


def unavailable_sample(*, sampled_at: datetime, reason: str) -> ResourceSample:
    return ResourceSample(
        sampled_at=sampled_at,
        metrics=(
            ResourceMetric(name="cpu", available=False, reason=reason),
            ResourceMetric(name="memory", available=False, reason=reason),
            ResourceMetric(name="output write rate", available=False, reason=reason),
        ),
        health=ResourceHealth.UNAVAILABLE,
        error=reason,
    )


def safe_sample_resources(
    sampler: ResourceSampler,
    *,
    clock: Callable[[], datetime] | None = None,
) -> ResourceSample:
    try:
        return sampler.sample()
    except Exception as exc:
        now = clock() if clock is not None else datetime.now(UTC)
        return unavailable_sample(sampled_at=now, reason=exc.__class__.__name__)


def sample_health(metrics: tuple[ResourceMetric, ...]) -> ResourceHealth:
    if not metrics or all(not metric.available for metric in metrics):
        return ResourceHealth.UNAVAILABLE
    if any(metric.health == ResourceHealth.CRITICAL for metric in metrics):
        return ResourceHealth.CRITICAL
    if any(metric.health == ResourceHealth.WARNING for metric in metrics):
        return ResourceHealth.WARNING
    return ResourceHealth.OK
