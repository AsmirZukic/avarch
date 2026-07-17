from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from avarch.application.job_control import JobControlWorkflowError
from avarch.application.job_views import JobDetails
from avarch.application.scheduler_snapshot import (
    SchedulerSnapshot,
    WatchJobDetailsSummary,
    WatchLogTailSummary,
)
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
    log_tail_lines: int = 80
    log_line_chars: int = 240
    selected_job_id: int | None = None
    cancel_confirmation: CancelConfirmation | None = None
    details_visible: bool = False
    logs_visible: bool = False
    details: WatchJobDetailsSummary | None = None
    log_tail: WatchLogTailSummary | None = None

    def refresh(self, snapshot: SchedulerSnapshot) -> None:
        active_ids = tuple(job.job_id for job in snapshot.active_jobs)
        if self.selected_job_id in active_ids:
            return
        if len(active_ids) == 1:
            self.selected_job_id = active_ids[0]
            return
        self.selected_job_id = None
        self.cancel_confirmation = None
        self.details = None
        self.log_tail = None

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

    def handle_details_key(self, *, controller: WatchController) -> WatchControlAction:
        if self.selected_job_id is None:
            return WatchControlAction(
                action="selection_required",
                message="Select an active job before showing details.",
            )
        self.details_visible = not self.details_visible
        if not self.details_visible:
            self.details = None
            return WatchControlAction(action="details_hidden", message="Job details hidden.")
        self.details = _details_summary(controller.details(job_id=self.selected_job_id))
        return WatchControlAction(action="details_visible", message="Job details visible.")

    def handle_logs_key(self, *, controller: WatchController) -> WatchControlAction:
        if self.selected_job_id is None:
            return WatchControlAction(
                action="selection_required",
                message="Select an active job before showing logs.",
            )
        self.logs_visible = not self.logs_visible
        if not self.logs_visible:
            self.log_tail = None
            return WatchControlAction(action="logs_hidden", message="Logs hidden.")
        paths = controller.log_paths(job_id=self.selected_job_id)
        path = paths.stderr_log or paths.stdout_log
        self.log_tail = read_bounded_log_tail(
            path,
            max_lines=self.log_tail_lines,
            max_line_chars=self.log_line_chars,
        )
        return WatchControlAction(action="logs_visible", message="Logs visible.")

    def apply_to_snapshot(self, snapshot: SchedulerSnapshot) -> SchedulerSnapshot:
        return snapshot.model_copy(
            update={
                "watch_details": self.details if self.details_visible else None,
                "watch_log_tail": self.log_tail if self.logs_visible else None,
            }
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


def read_bounded_log_tail(
    path: str | None,
    *,
    max_lines: int,
    max_line_chars: int,
) -> WatchLogTailSummary:
    if path is None:
        return WatchLogTailSummary(path=None, missing=True)
    log_path = Path(path)
    lines: deque[str] = deque(maxlen=max(0, max_lines))
    truncated = False
    try:
        with log_path.open("r", encoding="utf-8", errors="replace") as log_file:
            for raw_line in log_file:
                line = raw_line.rstrip("\r\n")
                if len(line) > max_line_chars:
                    line = line[:max_line_chars] + "..."
                    truncated = True
                lines.append(line)
    except OSError:
        return WatchLogTailSummary(path=path, missing=True)
    return WatchLogTailSummary(path=path, lines=tuple(lines), truncated=truncated)


def _details_summary(details: JobDetails | None) -> WatchJobDetailsSummary | None:
    if details is None or details.id is None:
        return None
    latest = details.attempt_history[-1] if details.attempt_history else None
    return WatchJobDetailsSummary(
        job_id=details.id,
        attempt_number=latest.attempt_number if latest is not None else None,
        source_path=details.source_path,
        profile_name=details.profile_name,
        status=details.status,
        stage=details.stage,
        started_at=details.started_at,
        plan_path=details.plan_path,
        output_path=details.output_path,
        stdout_log=latest.stdout_log if latest is not None else None,
        stderr_log=latest.stderr_log if latest is not None else None,
        last_error_type=details.last_error_type,
        last_error_message=details.last_error_message,
    )
