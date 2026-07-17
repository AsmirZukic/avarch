from __future__ import annotations

from datetime import UTC, datetime, timedelta

from avarch.application.resource_telemetry import (
    NullResourceSampler,
    ResourceHealth,
    ResourceMetric,
    ResourceSample,
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
    metrics = (
        ResourceMetric(name="cpu", value=95.0, unit="percent", health=ResourceHealth.OK),
    )

    assert sample_health(metrics) == ResourceHealth.OK
