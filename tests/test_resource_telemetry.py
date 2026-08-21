from __future__ import annotations

from datetime import UTC, datetime, timedelta

from avarch.application.resource_telemetry import (
    CompositeResourceSampler,
    NullResourceSampler,
    ResourceHealth,
    ResourceMetric,
    ResourceSample,
    ResourceTelemetryPolicy,
    apply_resource_health,
    safe_sample_resources,
    sample_health,
)


def test_first_sample_can_contain_unavailable_deltas() -> None:
    sample = ResourceSample(
        sampled_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        metrics=(
            ResourceMetric(
                name="cpu",
                available=False,
                reason="delta_unavailable",
                health=ResourceHealth.UNAVAILABLE,
            ),
            ResourceMetric(name="memory", value=1024, unit="bytes"),
        ),
    )

    assert sample.metrics[0].available is False
    assert sample.metrics[0].reason == "delta_unavailable"
    assert sample_health(sample.metrics) == ResourceHealth.OK


def test_sampling_failure_returns_unavailable_sample() -> None:
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)

    class BrokenSampler:
        def sample(self) -> ResourceSample:
            raise RuntimeError("boom")

    sample = safe_sample_resources(BrokenSampler(), clock=lambda: now)

    assert sample.sampled_at == now
    assert sample.health == ResourceHealth.UNAVAILABLE
    assert sample.error == "RuntimeError"
    assert all(not metric.available for metric in sample.metrics)


def test_unsupported_platform_returns_structured_unavailable_sample() -> None:
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    sampler = NullResourceSampler(reason="unsupported_platform", clock=lambda: now)

    sample = sampler.sample()

    assert sample.sampled_at == now
    assert sample.health == ResourceHealth.UNAVAILABLE
    assert [metric.name for metric in sample.metrics] == ["cpu", "memory", "output write rate"]
    assert {metric.reason for metric in sample.metrics} == {"unsupported_platform"}


def test_snapshot_timestamp_and_sample_timestamp_are_separate_values() -> None:
    sample_time = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    snapshot_time = sample_time + timedelta(seconds=5)
    sample = ResourceSample(sampled_at=sample_time, metrics=())

    assert sample.sampled_at != snapshot_time


def test_high_cpu_alone_is_not_warning_or_critical() -> None:
    metrics = (ResourceMetric(name="cpu", value=95.0, unit="percent", health=ResourceHealth.OK),)

    assert sample_health(metrics) == ResourceHealth.OK


def test_memory_warns_when_available_memory_approaches_reserve() -> None:
    sample = ResourceSample(
        sampled_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        metrics=(
            ResourceMetric(
                name="memory",
                value=9 * 1024**3,
                total=10 * 1024**3,
                unit="bytes",
            ),
        ),
    )

    classified = apply_resource_health(
        sample,
        policy=ResourceTelemetryPolicy(
            memory_reserve_bytes=2 * 1024**3,
            memory_critical_bytes=512 * 1024**2,
        ),
    )

    assert classified.health == ResourceHealth.WARNING
    assert classified.metrics[0].health == ResourceHealth.WARNING


def test_memory_is_critical_only_below_hard_safety_threshold() -> None:
    sample = ResourceSample(
        sampled_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        metrics=(
            ResourceMetric(
                name="memory",
                value=9 * 1024**3 + 400 * 1024**2,
                total=10 * 1024**3,
                unit="bytes",
            ),
        ),
    )
    policy = ResourceTelemetryPolicy(
        memory_reserve_bytes=2 * 1024**3,
        memory_critical_bytes=512 * 1024**2,
    )

    warning = apply_resource_health(sample, policy=policy)
    critical = apply_resource_health(
        ResourceSample(
            sampled_at=sample.sampled_at,
            metrics=(
                ResourceMetric(
                    name="memory",
                    value=9 * 1024**3 + 800 * 1024**2,
                    total=10 * 1024**3,
                    unit="bytes",
                ),
            ),
        ),
        policy=policy,
    )

    assert warning.health == ResourceHealth.WARNING
    assert critical.health == ResourceHealth.CRITICAL


def test_output_write_rate_is_informational_for_health() -> None:
    sample = ResourceSample(
        sampled_at=datetime(2026, 7, 17, 12, 0, tzinfo=UTC),
        metrics=(
            ResourceMetric(name="output write rate", value=500 * 1024**2, unit="bytes_per_second"),
        ),
    )

    classified = apply_resource_health(sample)

    assert classified.health == ResourceHealth.OK


def test_composite_sampler_merges_metrics_from_child_samplers() -> None:
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)

    class FixedSampler:
        def __init__(self, metric: ResourceMetric) -> None:
            self._metric = metric

        def sample(self) -> ResourceSample:
            return ResourceSample(sampled_at=now, metrics=(self._metric,))

    sampler = CompositeResourceSampler(
        (
            FixedSampler(ResourceMetric(name="cpu", value=25, unit="percent")),
            FixedSampler(
                ResourceMetric(name="output write rate", value=10, unit="bytes_per_second")
            ),
        ),
        clock=lambda: now,
    )

    sample = sampler.sample()

    assert [metric.name for metric in sample.metrics] == ["cpu", "output write rate"]
    assert sample.health == ResourceHealth.OK
