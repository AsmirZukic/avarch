from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
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
class ResourceTelemetryPolicy:
    memory_reserve_bytes: int = 2 * 1024 * 1024 * 1024
    memory_critical_bytes: int = 512 * 1024 * 1024


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


class CompositeResourceSampler:
    def __init__(
        self,
        samplers: tuple[ResourceSampler, ...],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._samplers = samplers
        self._clock = clock or (lambda: datetime.now(UTC))

    def sample(self) -> ResourceSample:
        if not self._samplers:
            return unavailable_sample(sampled_at=self._clock(), reason="unsupported")
        samples = tuple(
            safe_sample_resources(sampler, clock=self._clock) for sampler in self._samplers
        )
        sampled_at = max(sample.sampled_at for sample in samples)
        metrics = tuple(metric for sample in samples for metric in sample.metrics)
        errors = tuple(sample.error for sample in samples if sample.error is not None)
        return ResourceSample(
            sampled_at=sampled_at,
            metrics=metrics,
            health=sample_health(metrics),
            error="; ".join(errors) if errors else None,
        )


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


def apply_resource_health(
    sample: ResourceSample,
    *,
    policy: ResourceTelemetryPolicy | None = None,
) -> ResourceSample:
    policy = policy or ResourceTelemetryPolicy()
    metrics = tuple(_metric_with_health(metric, policy=policy) for metric in sample.metrics)
    return replace(sample, metrics=metrics, health=sample_health(metrics))


def sample_health(metrics: tuple[ResourceMetric, ...]) -> ResourceHealth:
    if not metrics or all(not metric.available for metric in metrics):
        return ResourceHealth.UNAVAILABLE
    if any(metric.health == ResourceHealth.CRITICAL for metric in metrics):
        return ResourceHealth.CRITICAL
    if any(metric.health == ResourceHealth.WARNING for metric in metrics):
        return ResourceHealth.WARNING
    return ResourceHealth.OK


def _metric_with_health(
    metric: ResourceMetric,
    *,
    policy: ResourceTelemetryPolicy,
) -> ResourceMetric:
    if metric.name != "memory" or not metric.available:
        return metric
    if metric.value is None or metric.total is None:
        return metric
    available_bytes = int(metric.total - metric.value)
    if available_bytes < policy.memory_critical_bytes:
        return replace(metric, health=ResourceHealth.CRITICAL)
    if available_bytes <= policy.memory_reserve_bytes:
        return replace(metric, health=ResourceHealth.WARNING)
    return metric
