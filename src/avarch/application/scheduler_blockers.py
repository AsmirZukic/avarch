from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from avarch.domain.scheduler import SchedulerMode


class JobEligibilityReason(StrEnum):
    SCHEDULER_PAUSED = "scheduler_paused"
    SCHEDULER_DRAINING = "scheduler_draining"
    JOB_HELD = "job_held"
    CANCEL_REQUESTED = "cancel_requested"
    SOURCE_MISSING = "source_missing"
    PLAN_MISSING = "plan_missing"
    PROFILE_MISSING = "profile_missing"
    TOOL_UNAVAILABLE = "tool_unavailable"
    INSUFFICIENT_STAGING_SPACE = "insufficient_staging_space"
    RETRY_BACKOFF = "retry_backoff"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class JobEligibilityDecision:
    launchable: bool
    reason: JobEligibilityReason | None = None
    details: Mapping[str, object] | None = None
    capacity_limited: bool = False


def decide_job_eligibility(
    *,
    scheduler_mode: SchedulerMode,
    held: bool = False,
    cancel_requested: bool = False,
    source_missing: bool = False,
    plan_missing: bool = False,
    profile_missing: bool = False,
    tool_unavailable: bool = False,
    insufficient_staging_space: bool = False,
    retry_backoff: bool = False,
    capacity_available: bool = True,
    details: Mapping[str, object] | None = None,
) -> JobEligibilityDecision:
    reason = _known_reason(
        scheduler_mode=scheduler_mode,
        held=held,
        cancel_requested=cancel_requested,
        source_missing=source_missing,
        plan_missing=plan_missing,
        profile_missing=profile_missing,
        tool_unavailable=tool_unavailable,
        insufficient_staging_space=insufficient_staging_space,
        retry_backoff=retry_backoff,
    )
    if reason is not None:
        return JobEligibilityDecision(launchable=False, reason=reason, details=details or {})
    if not capacity_available:
        return JobEligibilityDecision(
            launchable=False,
            reason=None,
            details=details or {},
            capacity_limited=True,
        )
    return JobEligibilityDecision(launchable=True, details=details or {})


def _known_reason(
    *,
    scheduler_mode: SchedulerMode,
    held: bool,
    cancel_requested: bool,
    source_missing: bool,
    plan_missing: bool,
    profile_missing: bool,
    tool_unavailable: bool,
    insufficient_staging_space: bool,
    retry_backoff: bool,
) -> JobEligibilityReason | None:
    if scheduler_mode == SchedulerMode.PAUSED:
        return JobEligibilityReason.SCHEDULER_PAUSED
    if scheduler_mode == SchedulerMode.DRAINING:
        return JobEligibilityReason.SCHEDULER_DRAINING
    if held:
        return JobEligibilityReason.JOB_HELD
    if cancel_requested:
        return JobEligibilityReason.CANCEL_REQUESTED
    if source_missing:
        return JobEligibilityReason.SOURCE_MISSING
    if plan_missing:
        return JobEligibilityReason.PLAN_MISSING
    if profile_missing:
        return JobEligibilityReason.PROFILE_MISSING
    if tool_unavailable:
        return JobEligibilityReason.TOOL_UNAVAILABLE
    if insufficient_staging_space:
        return JobEligibilityReason.INSUFFICIENT_STAGING_SPACE
    if retry_backoff:
        return JobEligibilityReason.RETRY_BACKOFF
    if scheduler_mode == SchedulerMode.STOPPING:
        return JobEligibilityReason.UNKNOWN
    return None
