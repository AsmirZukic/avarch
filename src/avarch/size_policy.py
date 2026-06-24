from __future__ import annotations

from enum import StrEnum

from avarch.profiles.models import EncodingProfile


class SizeDecision(StrEnum):
    ACCEPT = "accept"
    REJECT_NOT_SMALLER = "reject_not_smaller"
    REJECT_MINIMUM_SAVINGS_NOT_MET = "reject_minimum_savings_not_met"


def evaluate_size_policy(
    original_size: int,
    encoded_size: int,
    profile: EncodingProfile,
) -> SizeDecision:
    if original_size < 0 or encoded_size < 0:
        raise ValueError("File sizes must be non-negative.")
    if profile.promotion.require_smaller and encoded_size >= original_size:
        return SizeDecision.REJECT_NOT_SMALLER
    if original_size == 0:
        return SizeDecision.ACCEPT

    savings_percent = ((original_size - encoded_size) / original_size) * 100
    if savings_percent < profile.promotion.minimum_savings_percent:
        return SizeDecision.REJECT_MINIMUM_SAVINGS_NOT_MET
    return SizeDecision.ACCEPT


def outcome_reason_for_size_decision(decision: SizeDecision) -> str:
    if decision == SizeDecision.REJECT_NOT_SMALLER:
        return "skipped_size_not_smaller"
    if decision == SizeDecision.REJECT_MINIMUM_SAVINGS_NOT_MET:
        return "skipped_minimum_savings_not_met"
    raise ValueError(f"Size decision does not have a rejection reason: {decision}")