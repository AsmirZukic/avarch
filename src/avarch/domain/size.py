from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SizeDecision(StrEnum):
    ACCEPT = "accept"
    REJECT_NOT_SMALLER = "reject_not_smaller"
    REJECT_MINIMUM_SAVINGS_NOT_MET = "reject_minimum_savings_not_met"


@dataclass(frozen=True, slots=True)
class SizePolicy:
    require_smaller: bool
    minimum_savings_percent: float


def evaluate_size_policy(
    original_size: int,
    encoded_size: int,
    policy: SizePolicy,
) -> SizeDecision:
    if original_size < 0 or encoded_size < 0:
        raise ValueError("File sizes must be non-negative.")
    if policy.require_smaller and encoded_size >= original_size:
        return SizeDecision.REJECT_NOT_SMALLER
    if original_size == 0:
        return SizeDecision.ACCEPT

    savings_percent = ((original_size - encoded_size) / original_size) * 100
    if savings_percent < policy.minimum_savings_percent:
        return SizeDecision.REJECT_MINIMUM_SAVINGS_NOT_MET
    return SizeDecision.ACCEPT

