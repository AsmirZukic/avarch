from __future__ import annotations

import pytest

from avarch.application.scheduler_blockers import (
    JobEligibilityReason,
    decide_job_eligibility,
)
from avarch.domain.scheduler import SchedulerMode


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"scheduler_mode": SchedulerMode.PAUSED}, JobEligibilityReason.SCHEDULER_PAUSED),
        ({"scheduler_mode": SchedulerMode.DRAINING}, JobEligibilityReason.SCHEDULER_DRAINING),
        ({"held": True}, JobEligibilityReason.JOB_HELD),
        ({"cancel_requested": True}, JobEligibilityReason.CANCEL_REQUESTED),
        ({"source_missing": True}, JobEligibilityReason.SOURCE_MISSING),
        ({"plan_missing": True}, JobEligibilityReason.PLAN_MISSING),
        ({"profile_missing": True}, JobEligibilityReason.PROFILE_MISSING),
        ({"tool_unavailable": True}, JobEligibilityReason.TOOL_UNAVAILABLE),
        (
            {"insufficient_staging_space": True},
            JobEligibilityReason.INSUFFICIENT_STAGING_SPACE,
        ),
        ({"retry_backoff": True}, JobEligibilityReason.RETRY_BACKOFF),
    ],
)
def test_known_blocker_reasons(kwargs: dict[str, object], reason: JobEligibilityReason) -> None:
    scheduler_mode = kwargs.pop("scheduler_mode", SchedulerMode.RUNNING)
    decision = decide_job_eligibility(
        scheduler_mode=scheduler_mode,
        details={"job_id": 1},
        **kwargs,
    )

    assert decision.launchable is False
    assert decision.capacity_limited is False
    assert decision.reason == reason
    assert decision.details == {"job_id": 1}


def test_capacity_limited_is_not_known_blocker() -> None:
    decision = decide_job_eligibility(
        scheduler_mode=SchedulerMode.RUNNING,
        capacity_available=False,
    )

    assert decision.launchable is False
    assert decision.capacity_limited is True
    assert decision.reason is None


def test_unknown_blocker_is_explicit() -> None:
    decision = decide_job_eligibility(scheduler_mode=SchedulerMode.STOPPING)

    assert decision.launchable is False
    assert decision.capacity_limited is False
    assert decision.reason == JobEligibilityReason.UNKNOWN


def test_launchable_job_has_no_reason() -> None:
    decision = decide_job_eligibility(scheduler_mode=SchedulerMode.RUNNING)

    assert decision.launchable is True
    assert decision.reason is None
    assert decision.capacity_limited is False
