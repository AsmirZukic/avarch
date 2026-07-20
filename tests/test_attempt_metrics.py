from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from avarch.application.attempt_metrics import (
    ATTEMPT_METRICS_SCHEMA_VERSION,
    AttemptMetricsAccumulator,
    AttemptResourceSample,
)
from avarch.domain.jobs import AttemptStatus
from avarch.domain.progress import ProgressPhase, ProgressSnapshot, ProgressSource, ProgressUnit
from avarch.models.execution import ProcessResourceSummary


def test_attempt_metrics_aggregate_progress_and_resources_after_warmup() -> None:
    started = datetime(2026, 7, 1, 12, tzinfo=UTC)
    accumulator = AttemptMetricsAccumulator(warmup=timedelta(seconds=10))

    accumulator.record_progress(_snapshot(started, frames=0))
    accumulator.record_progress(_snapshot(started + timedelta(seconds=5), frames=25))
    accumulator.record_resource_sample(
        AttemptResourceSample(
            observed_at=started + timedelta(seconds=5),
            rss_bytes=900,
            cgroup_memory_current_bytes=1000,
            cpu_utilization_percent=50.0,
            swap_current_bytes=100,
            cpu_throttled_events=2,
            cpu_throttled_usec=200,
        )
    )
    accumulator.record_progress(_snapshot(started + timedelta(seconds=10), frames=50))
    accumulator.record_resource_sample(
        AttemptResourceSample(
            observed_at=started + timedelta(seconds=10),
            rss_bytes=1500,
            cgroup_memory_current_bytes=2500,
            cpu_utilization_percent=80.0,
            swap_current_bytes=120,
            cpu_throttled_events=3,
            cpu_throttled_usec=260,
        )
    )
    accumulator.record_progress(_snapshot(started + timedelta(seconds=40), frames=200))
    accumulator.record_resource_sample(
        AttemptResourceSample(
            observed_at=started + timedelta(seconds=40),
            rss_bytes=1400,
            cgroup_memory_current_bytes=2400,
            cpu_utilization_percent=60.0,
            swap_current_bytes=180,
            cpu_throttled_events=5,
            cpu_throttled_usec=900,
        )
    )

    summary = accumulator.finalize(status=AttemptStatus.COMPLETED)

    assert summary.schema_version == ATTEMPT_METRICS_SCHEMA_VERSION
    assert summary.total_frames == 200
    assert summary.observation_duration_seconds == 30
    assert summary.aggregate_fps == 5.0
    assert summary.peak_rss_bytes == 1500
    assert summary.peak_cgroup_memory_bytes == 2500
    assert summary.average_cpu_utilization_percent == 70.0
    assert summary.swap_current_bytes_delta == 60
    assert summary.cpu_throttled_events_delta == 2
    assert summary.cpu_throttled_usec_delta == 640
    assert summary.incomplete is False
    assert summary.progress_samples_observed == 2
    assert summary.resource_samples_observed == 2
    assert summary.warmup_seconds == 10


def test_attempt_metrics_uses_process_summary_for_final_pressure_deltas() -> None:
    started = datetime(2026, 7, 1, 12, tzinfo=UTC)
    accumulator = AttemptMetricsAccumulator()
    accumulator.record_progress(_snapshot(started, frames=10))
    accumulator.record_progress(_snapshot(started + timedelta(seconds=2), frames=30))
    accumulator.record_resource_sample(
        AttemptResourceSample(
            observed_at=started,
            rss_bytes=1000,
            cgroup_memory_current_bytes=1200,
        )
    )
    accumulator.record_resource_summary(
        ProcessResourceSummary(
            peak_rss_bytes=2048,
            peak_swap_bytes=128,
            memory_oom_events_delta=1,
            memory_oom_kill_events_delta=0,
            swap_current_bytes_delta=64,
            cpu_throttled_events_delta=7,
            cpu_throttled_usec_delta=800,
            attribution_available=True,
        )
    )

    summary = accumulator.finalize(status=AttemptStatus.FAILED)

    assert summary.peak_rss_bytes == 2048
    assert summary.memory_oom_events_delta == 1
    assert summary.memory_oom_kill_events_delta == 0
    assert summary.swap_current_bytes_delta == 64
    assert summary.cpu_throttled_events_delta == 7
    assert summary.cpu_throttled_usec_delta == 800
    assert summary.resource_attribution_available is True
    assert summary.incomplete is True


def test_attempt_metrics_handles_missing_first_cpu_sample_and_partial_telemetry() -> None:
    started = datetime(2026, 7, 1, 12, tzinfo=UTC)
    accumulator = AttemptMetricsAccumulator()
    accumulator.record_progress(_snapshot(started, frames=5))
    accumulator.record_progress(_snapshot(started + timedelta(seconds=10), frames=5))
    accumulator.record_resource_sample(AttemptResourceSample(observed_at=started))
    accumulator.record_resource_sample(
        AttemptResourceSample(
            observed_at=started + timedelta(seconds=10),
            cpu_utilization_percent=75.0,
        )
    )

    summary = accumulator.finalize(status=AttemptStatus.CANCELED)

    assert summary.aggregate_fps is None
    assert summary.peak_rss_bytes is None
    assert summary.peak_cgroup_memory_bytes is None
    assert summary.average_cpu_utilization_percent == 75.0
    assert summary.swap_current_bytes_delta is None
    assert summary.cpu_throttled_events_delta is None
    assert summary.incomplete is True


def test_attempt_metrics_rejects_out_of_order_resource_samples() -> None:
    started = datetime(2026, 7, 1, 12, tzinfo=UTC)
    accumulator = AttemptMetricsAccumulator()
    accumulator.record_resource_sample(AttemptResourceSample(observed_at=started))

    with pytest.raises(ValueError, match="observed_at order"):
        accumulator.record_resource_sample(
            AttemptResourceSample(observed_at=started - timedelta(seconds=1))
        )


def _snapshot(
    observed_at: datetime,
    *,
    frames: int,
    phase: ProgressPhase = ProgressPhase.ENCODING,
) -> ProgressSnapshot:
    return ProgressSnapshot(
        phase=phase,
        current=frames,
        total=200,
        unit=ProgressUnit.FRAMES,
        rate_per_second=None,
        speed_ratio=None,
        source=ProgressSource.AV1AN_STRUCTURED,
        message=None,
        phase_started_at=observed_at,
        observed_at=observed_at,
        heartbeat_at=observed_at,
        advanced_at=observed_at,
    )
