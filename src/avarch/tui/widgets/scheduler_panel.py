from __future__ import annotations

from avarch.tui.models.dashboard import SchedulerSummary


def scheduler_summary_text(scheduler: SchedulerSummary) -> str:
    return (
        "Scheduler\n"
        f"  Mode: {scheduler.mode.upper()}\n"
        f"  Lease: {scheduler.lease_state}\n"
        f"  Runner: {scheduler.runner_id or 'none'}\n"
        f"  Cancel pending: {scheduler.cancel_pending}\n"
        f"  Hold pending: {scheduler.hold_pending}"
    )
