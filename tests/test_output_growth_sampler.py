from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from avarch.adapters.system.resource_telemetry import OutputGrowthSampler
from avarch.application.resource_telemetry import ResourceMetric, ResourceSample


def test_output_write_rate_uses_file_growth_between_samples(tmp_path: Path) -> None:
    output = tmp_path / "movie.av1.mkv"
    output.write_bytes(b"a" * 100)
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    times = iter([now, now + timedelta(seconds=2)])
    sampler = OutputGrowthSampler(paths=lambda: (output,), clock=lambda: next(times))

    sampler.sample()
    output.write_bytes(b"a" * 300)
    sample = sampler.sample()

    assert _metric(sample).name == "output write rate"
    assert _metric(sample).value == 100.0
    assert _metric(sample).unit == "bytes_per_second"


def test_output_write_rate_is_zero_when_file_is_unchanged(tmp_path: Path) -> None:
    output = tmp_path / "movie.av1.mkv"
    output.write_bytes(b"a" * 100)
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    times = iter([now, now + timedelta(seconds=1)])
    sampler = OutputGrowthSampler(paths=lambda: (output,), clock=lambda: next(times))

    sampler.sample()
    sample = sampler.sample()

    assert _metric(sample).value == 0.0


def test_output_write_rate_resets_when_file_is_replaced(tmp_path: Path) -> None:
    output = tmp_path / "movie.av1.mkv"
    replacement = tmp_path / "replacement.mkv"
    output.write_bytes(b"a" * 300)
    replacement.write_bytes(b"b" * 50)
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    times = iter([now, now + timedelta(seconds=1)])
    sampler = OutputGrowthSampler(paths=lambda: (output,), clock=lambda: next(times))

    sampler.sample()
    replacement.replace(output)
    sample = sampler.sample()

    assert _metric(sample).value == 0.0


def test_output_write_rate_sums_multiple_active_attempts(tmp_path: Path) -> None:
    first = tmp_path / "first.mkv"
    second = tmp_path / "second.mkv"
    first.write_bytes(b"a" * 100)
    second.write_bytes(b"b" * 200)
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    times = iter([now, now + timedelta(seconds=2)])
    sampler = OutputGrowthSampler(paths=lambda: (first, second), clock=lambda: next(times))

    sampler.sample()
    first.write_bytes(b"a" * 160)
    second.write_bytes(b"b" * 260)
    sample = sampler.sample()

    assert _metric(sample).value == 60.0


def test_output_write_rate_handles_path_disappearing_during_promotion(tmp_path: Path) -> None:
    output = tmp_path / "movie.av1.mkv"
    output.write_bytes(b"a" * 100)
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    times = iter([now, now + timedelta(seconds=1)])
    sampler = OutputGrowthSampler(paths=lambda: (output,), clock=lambda: next(times))

    sampler.sample()
    output.unlink()
    sample = sampler.sample()

    assert _metric(sample).available is True
    assert _metric(sample).value == 0.0


def test_output_write_rate_counter_cannot_become_negative(tmp_path: Path) -> None:
    output = tmp_path / "movie.av1.mkv"
    output.write_bytes(b"a" * 300)
    now = datetime(2026, 7, 17, 12, 0, tzinfo=UTC)
    times = iter([now, now + timedelta(seconds=1)])
    sampler = OutputGrowthSampler(paths=lambda: (output,), clock=lambda: next(times))

    sampler.sample()
    output.write_bytes(b"a" * 100)
    sample = sampler.sample()

    assert _metric(sample).value == 0.0


def _metric(sample: ResourceSample) -> ResourceMetric:
    return next(metric for metric in sample.metrics if metric.name == "output write rate")
