from __future__ import annotations

from dataclasses import dataclass

from avarch.application.benchmark_samples import (
    BenchmarkSamplePolicy,
    BenchmarkSampleReason,
    select_representative_sample,
)
from avarch.application.performance_candidates import generate_safe_concurrency_candidates
from avarch.domain.resource_policy import parse_resource_intent
from avarch.domain.scheduler import ResourceCapacity


def test_sample_selection_is_deterministic_from_identity() -> None:
    candidates = _candidates()

    first = select_representative_sample(
        duration_seconds=600,
        frame_rate=24,
        identity_seed="plan-a",
        candidates=candidates,
    )
    second = select_representative_sample(
        duration_seconds=600,
        frame_rate=24,
        identity_seed="plan-a",
        candidates=candidates,
    )

    assert first.sample == second.sample
    assert first.reason == BenchmarkSampleReason.SELECTED
    assert first.sample is not None
    assert first.sample.estimated_frames == 720


def test_short_or_unseekable_inputs_skip_sampling() -> None:
    candidates = _candidates()

    short = select_representative_sample(
        duration_seconds=15,
        frame_rate=24,
        identity_seed="short",
        candidates=candidates,
    )
    unseekable = select_representative_sample(
        duration_seconds=600,
        frame_rate=24,
        identity_seed="stream",
        candidates=candidates,
        seekable=False,
    )

    assert short.reason == BenchmarkSampleReason.TOO_SHORT
    assert short.sample is None
    assert unseekable.reason == BenchmarkSampleReason.NOT_SEEKABLE
    assert unseekable.sample is None


def test_sample_reduces_candidates_it_cannot_exercise_fairly() -> None:
    candidates = _candidates(cpu_budget=16)

    selection = select_representative_sample(
        duration_seconds=120,
        frame_rate=24,
        identity_seed="small-sample",
        candidates=candidates,
        policy=BenchmarkSamplePolicy(target_sample_seconds=10, frames_per_parallel_unit=60),
    )

    assert selection.sample is not None
    assert selection.sample.parallel_units == 4
    assert all(
        candidate.workers == "auto" or candidate.workers <= 4
        for candidate in selection.candidates
    )


def test_custom_pipeline_skips_sampling() -> None:
    selection = select_representative_sample(
        duration_seconds=600,
        frame_rate=24,
        identity_seed="custom",
        candidates=_candidates(),
        custom_pipeline=True,
    )

    assert selection.reason == BenchmarkSampleReason.CUSTOM_PIPELINE
    assert selection.candidates == ()


def _candidates(cpu_budget: int = 8):
    return generate_safe_concurrency_candidates(
        intent=parse_resource_intent(workers="auto"),
        capacity=ResourceCapacity(
            cheap_workers=1,
            av1an_jobs=1,
            file_ops=1,
            cpu_budget=cpu_budget,
            memory_budget_bytes=32 * 1024**3,
        ),
        observations=(_Observation(peak_memory_bytes=1 * 1024**3),),
    )


@dataclass(frozen=True, slots=True)
class _Observation:
    peak_memory_bytes: int | None
