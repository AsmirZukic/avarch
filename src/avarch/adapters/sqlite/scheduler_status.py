from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlmodel import Session, select

from avarch.adapters.sqlite import scheduler_state as scheduler_state_adapter
from avarch.adapters.sqlite.models import MediaFile
from avarch.adapters.sqlite.scheduler_state import (
    active_scheduler_jobs,
    get_or_create_scheduler_state,
    job_counts_by_status,
    pending_cancel_count,
    pending_hold_count,
)
from avarch.application.scheduler_status import ActiveSchedulerJob, SchedulerStatusView
from avarch.domain.scheduler import SchedulerMode


class SqliteSchedulerStatusStore:
    def __init__(self, session: Session) -> None:
        self._session = session

    def scheduler_status(self, *, now: datetime) -> SchedulerStatusView:
        state = get_or_create_scheduler_state(self._session, now=now)
        media_by_id = {
            media_file.id: media_file
            for media_file in self._session.exec(select(MediaFile)).all()
            if media_file.id is not None
        }
        lease_state = "inactive"
        if state.runner_id is not None:
            lease_state = (
                "active" if scheduler_state_adapter.lease_active(state, now=now) else "stale"
            )
        return SchedulerStatusView(
            mode=SchedulerMode(state.mode),
            runner_id=state.runner_id,
            lease_state=lease_state,
            counts_by_status=job_counts_by_status(self._session),
            cancel_pending=pending_cancel_count(self._session),
            hold_pending=pending_hold_count(self._session),
            active_jobs=[
                ActiveSchedulerJob(
                    job_id=job.id,
                    stage=job.stage,
                    file_name=_file_name(media_by_id.get(job.media_file_id)),
                )
                for job in active_scheduler_jobs(self._session)
            ],
        )


def _file_name(media_file: MediaFile | None) -> str:
    return Path(media_file.path).name if media_file is not None else "<missing>"
