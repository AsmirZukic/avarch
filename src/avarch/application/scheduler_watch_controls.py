from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from avarch.application.job_control import JobControlWorkflowError
from avarch.application.scheduler_snapshot import SchedulerSnapshot
from avarch.application.scheduler_watch_controller import WatchController
from avarch.application.scheduler_watch_keys import KEY_CANCEL


@dataclass(frozen=True, slots=True)
class CancelConfirmation:
    job_id: int
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class WatchControlAction:
    action: str
    message: str
    attached: bool = True


@dataclass(slots=True)
class SchedulerWatchControlState:
    confirmation_ttl_seconds: int = 10
    selected_job_id: int | None = None
    cancel_confirmation: CancelConfirmation | None = None

    def refresh(self, snapshot: SchedulerSnapshot) -> None:
        active_ids = tuple(job.job_id for job in snapshot.active_jobs)
        if self.selected_job_id in active_ids:
            return
        if len(active_ids) == 1:
            self.selected_job_id = active_ids[0]
            return
        self.selected_job_id = None
        self.cancel_confirmation = None

    def select_job(self, *, job_id: int, snapshot: SchedulerSnapshot) -> WatchControlAction:
        active_ids = {job.job_id for job in snapshot.active_jobs}
        if job_id not in active_ids:
            self.selected_job_id = None
            self.cancel_confirmation = None
            return WatchControlAction(
                action="selection_missing",
                message=f"Job {job_id} is not active.",
            )
        self.selected_job_id = job_id
        self.cancel_confirmation = None
        return WatchControlAction(action="selected", message=f"Selected job {job_id}.")

    def handle_key(
        self,
        key: str,
        *,
        snapshot: SchedulerSnapshot,
        now: datetime,
    ) -> WatchControlAction | None:
        self.refresh(snapshot)
        if key != KEY_CANCEL:
            return None
        if self.selected_job_id is None:
            return WatchControlAction(
                action="selection_required",
                message="Select an active job before cancelling.",
            )
        self.cancel_confirmation = CancelConfirmation(
            job_id=self.selected_job_id,
            expires_at=now + timedelta(seconds=self.confirmation_ttl_seconds),
        )
        return WatchControlAction(
            action="confirm_cancel",
            message=f"Confirm cancellation for job {self.selected_job_id}.",
        )

    def confirm_cancel(
        self,
        *,
        controller: WatchController,
        now: datetime,
    ) -> WatchControlAction:
        confirmation = self.cancel_confirmation
        if confirmation is None:
            return WatchControlAction(action="no_confirmation", message="No cancellation pending.")
        if now > confirmation.expires_at:
            self.cancel_confirmation = None
            return WatchControlAction(
                action="confirmation_expired",
                message="Cancellation confirmation expired.",
            )
        self.cancel_confirmation = None
        try:
            controller.cancel(job_id=confirmation.job_id, reason="watch")
        except JobControlWorkflowError as exc:
            return WatchControlAction(action="cancel_conflict", message=str(exc), attached=True)
        return WatchControlAction(
            action="cancel_requested",
            message=f"Job {confirmation.job_id} cancellation requested.",
            attached=True,
        )

    def dismiss_confirmation(self) -> WatchControlAction:
        self.cancel_confirmation = None
        return WatchControlAction(action="dismissed", message="Cancellation dismissed.")
