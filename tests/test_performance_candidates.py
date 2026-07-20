from __future__ import annotations

from dataclasses import dataclass

from avarch.application.performance_candidates import generate_safe_concurrency_candidates
from avarch.domain.resource_policy import parse_resource_intent
from avarch.domain.scheduler import ResourceCapacity


def test_native_auto_baseline_is_always_present_for_auto_intent() -> None:
    candidates = generate_safe_concurrency_candidates(
        intent=parse_resource_intent(workers="auto"),
        capacity=ResourceCapacity(
            cheap_workers=1,
            av1an_jobs=1,
            file_ops=1,
            cpu_budget=8,
            memory_budget_bytes=16 * 1024**3,
        ),
        observations=(_Observation(peak_memory_bytes=2 * 1024**3),),
    )

    assert candidates[0].workers == "auto"
    assert candidates[0].svt_lp == "native"
    assert candidates[0].reservation.exclusive is True


def test_manual_configuration_produces_no_alternative_candidates() -> None:
    candidates = generate_safe_concurrency_candidates(
        intent=parse_resource_intent(mode="manual", workers=2, svt_lp=4),
        capacity=ResourceCapacity(
            cheap_workers=1,
            av1an_jobs=1,
            file_ops=1,
            cpu_budget=16,
            memory_budget_bytes=16 * 1024**3,
        ),
        observations=(_Observation(peak_memory_bytes=2 * 1024**3),),
    )

    assert [(candidate.workers, candidate.svt_lp) for candidate in candidates] == [(2, 4)]
    assert candidates[0].reservation.cpu == 8
    assert candidates[0].reservation.exclusive is False


def test_candidates_respect_cpu_budget_and_available_parallel_units() -> None:
    candidates = generate_safe_concurrency_candidates(
        intent=parse_resource_intent(workers="auto"),
        capacity=ResourceCapacity(
            cheap_workers=1,
            av1an_jobs=1,
            file_ops=1,
            cpu_budget=6,
            memory_budget_bytes=16 * 1024**3,
        ),
        observations=(_Observation(peak_memory_bytes=1 * 1024**3),),
        available_parallel_units=3,
    )

    generated = [candidate for candidate in candidates if candidate.workers != "auto"]

    assert generated
    assert all(
        isinstance(candidate.workers, int)
        and isinstance(candidate.svt_lp, int)
        and candidate.workers <= 3
        and candidate.workers * candidate.svt_lp <= 6
        for candidate in generated
    )


def test_candidates_exclude_known_memory_infeasible_alternatives() -> None:
    candidates = generate_safe_concurrency_candidates(
        intent=parse_resource_intent(workers="auto"),
        capacity=ResourceCapacity(
            cheap_workers=1,
            av1an_jobs=1,
            file_ops=1,
            cpu_budget=8,
            memory_budget_bytes=2 * 1024**3,
        ),
        observations=(_Observation(peak_memory_bytes=2 * 1024**3),),
    )

    assert [(candidate.workers, candidate.svt_lp) for candidate in candidates] == [
        ("auto", "native")
    ]


def test_candidate_set_is_deduplicated_and_bounded() -> None:
    candidates = generate_safe_concurrency_candidates(
        intent=parse_resource_intent(workers="auto"),
        capacity=ResourceCapacity(
            cheap_workers=1,
            av1an_jobs=1,
            file_ops=1,
            cpu_budget=32,
            memory_budget_bytes=64 * 1024**3,
        ),
        observations=(_Observation(peak_memory_bytes=1 * 1024**3),),
        max_candidates=5,
    )

    keys = [(candidate.workers, candidate.svt_lp) for candidate in candidates]

    assert len(candidates) == 5
    assert len(keys) == len(set(keys))


def test_default_eight_candidate_sweep_covers_balanced_and_extreme_topologies() -> None:
    candidates = generate_safe_concurrency_candidates(
        intent=parse_resource_intent(workers="auto"),
        capacity=ResourceCapacity(
            cheap_workers=1,
            av1an_jobs=1,
            file_ops=1,
            cpu_budget=19,
            memory_budget_bytes=16 * 1024**3,
        ),
        observations=(),
    )

    assert [(candidate.workers, candidate.svt_lp) for candidate in candidates] == [
        ("auto", "native"),
        (4, 4),
        (5, 3),
        (3, 6),
        (6, 3),
        (7, 2),
        (8, 2),
        (2, 9),
    ]


@dataclass(frozen=True, slots=True)
class _Observation:
    peak_memory_bytes: int | None
