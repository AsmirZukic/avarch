from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlmodel import Session, select

from avarch.adapters.sqlite.models import MediaFile
from avarch.application.scheduler_status import ActiveSchedulerJob, SchedulerStatusView
from avarch.scheduler_runner import scheduler_status


class SqliteSchedulerStatusStore:
    def __init__(self, session: Session) -> None:
        self._session = session

    def scheduler_status(self, *, now: datetime) -> SchedulerStatusView:
        status = scheduler_status(self._session, now=now)
        media_by_id = {
            media_file.id: media_file
            for media_file in self._session.exec(select(MediaFile)).all()
            if media_file.id is not None
        }
        return SchedulerStatusView(
            mode=status.mode,
            runner_id=status.runner_id,
            lease_state=status.lease_state,
            counts_by_status=status.counts_by_status,
            cancel_pending=status.cancel_pending,
            hold_pending=status.hold_pending,
            active_jobs=[
                ActiveSchedulerJob(
                    job_id=job.id,
                    stage=job.stage,
                    file_name=_file_name(media_by_id.get(job.media_file_id)),
                )
                for job in status.active_jobs
            ],
        )


def _file_name(media_file: MediaFile | None) -> str:
    return Path(media_file.path).name if media_file is not None else "<missing>"
