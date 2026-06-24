from __future__ import annotations

from avarch.domain.size import SizeDecision, SizePolicy, evaluate_size_policy


def test_rejects_larger_output() -> None:
    assert evaluate_size_policy(1000, 1001, _policy()) == SizeDecision.REJECT_NOT_SMALLER


def test_rejects_same_size_output() -> None:
    assert evaluate_size_policy(1000, 1000, _policy()) == SizeDecision.REJECT_NOT_SMALLER


def test_rejects_output_below_minimum_savings() -> None:
    assert evaluate_size_policy(1000, 999, _policy()) == SizeDecision.REJECT_MINIMUM_SAVINGS_NOT_MET


def test_accepts_output_above_minimum_savings() -> None:
    assert evaluate_size_policy(1000, 900, _policy()) == SizeDecision.ACCEPT


def test_allows_zero_minimum_savings_when_configured() -> None:
    assert (
        evaluate_size_policy(
            1000,
            999,
            _policy(minimum_savings_percent=0),
        )
        == SizeDecision.ACCEPT
    )


def _policy(*, require_smaller: bool = True, minimum_savings_percent: float = 5.0) -> SizePolicy:
    return SizePolicy(
        require_smaller=require_smaller,
        minimum_savings_percent=minimum_savings_percent,
    )
