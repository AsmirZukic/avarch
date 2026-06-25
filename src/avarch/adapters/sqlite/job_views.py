from __future__ import annotations

from pathlib import Path

from sqlmodel import Session, col, select

from avarch.adapters.sqlite.models import Job, JobAttempt, JobEvent, MediaFile
from avarch.application.job_views import (
    JobAttemptView,
    JobDetails,
    JobEventView,
    JobListItem,
)
from avarch.domain.jobs import JobStage, JobStatus


class SqliteJobViewStore:
    def __init__(self, session: Session) -> None:
        self._session = session

    def list_jobs(
        self,
        *,
        statuses: set[JobStatus] | None,
        stages: set[JobStage] | None,
        profile: str | None,
        limit: int | None,
    ) -> list[JobListItem]:
        statement = select(Job).order_by(
            col(Job.priority).desc(),
            col(Job.created_at).asc(),
            col(Job.id).asc(),
        )
        if statuses is not None:
            statement = statement.where(col(Job.status).in_(statuses))
        if stages is not None:
            statement = statement.where(col(Job.stage).in_(stages))
        if profile is not None:
            statement = statement.where(Job.profile_name == profile)
        if limit is not None:
            statement = statement.limit(limit)
        jobs = list(self._session.exec(statement).all())
        media_by_id = self._media_by_id()
        return [_list_item(job, media_by_id.get(job.media_file_id)) for job in jobs]

    def job_details(self, *, job_id: int) -> JobDetails | None:
        job = self._session.get(Job, job_id)
        if job is None:
            return None
        media_file = self._session.get(MediaFile, job.media_file_id)
        attempts = list(
            self._session.exec(
                select(JobAttempt)
                .where(JobAttempt.job_id == job_id)
                .order_by(col(JobAttempt.attempt_number).asc())
            ).all()
        )
        events = list(
            self._session.exec(
                select(JobEvent).where(JobEvent.job_id == job_id).order_by(col(JobEvent.id).asc())
            ).all()
        )
        return JobDetails(
            id=job.id,
            source_path=media_file.path if media_file is not None else "<missing>",
            profile_name=job.profile_name,
            profile_hash=job.profile_hash,
            queue_key=job.queue_key,
            priority=job.priority,
            status=JobStatus(job.status),
            stage=JobStage(job.stage),
            claimed_by=job.claimed_by,
            attempts=job.attempts,
            created_at=job.created_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
            cancel_requested_at=job.cancel_requested_at,
            hold_requested_at=job.hold_requested_at,
            probe_hash=job.probe_hash,
            plan_hash=job.plan_hash,
            plan_path=job.plan_path,
            output_path=job.output_path,
            latest_validation_id=job.latest_validation_id,
            latest_promotion_id=job.latest_promotion_id,
            last_error_type=job.last_error_type,
            last_error_message=job.last_error_message,
            attempt_history=[_attempt_view(attempt) for attempt in attempts],
            events=[_event_view(event) for event in events],
        )

    def latest_attempt(
        self,
        *,
        job_id: int,
        attempt_number: int | None,
    ) -> JobAttemptView | None:
        statement = select(JobAttempt).where(JobAttempt.job_id == job_id)
        if attempt_number is not None:
            statement = statement.where(JobAttempt.attempt_number == attempt_number)
        statement = statement.order_by(col(JobAttempt.attempt_number).desc())
        attempt = self._session.exec(statement).first()
        return _attempt_view(attempt) if attempt is not None else None

    def _media_by_id(self) -> dict[int, MediaFile]:
        return {
            media_file.id: media_file
            for media_file in self._session.exec(select(MediaFile)).all()
            if media_file.id is not None
        }


def _list_item(job: Job, media_file: MediaFile | None) -> JobListItem:
    return JobListItem(
        id=job.id,
        status=JobStatus(job.status),
        stage=JobStage(job.stage),
        priority=job.priority,
        attempts=job.attempts,
        profile_name=job.profile_name,
        file_name=Path(media_file.path).name if media_file is not None else "<missing>",
        cancel_requested_at=job.cancel_requested_at,
        hold_requested_at=job.hold_requested_at,
        last_error_message=job.last_error_message,
        skip_reason=job.skip_reason,
        outcome_reason=job.outcome_reason,
    )


def _attempt_view(attempt: JobAttempt) -> JobAttemptView:
    return JobAttemptView(
        attempt_number=attempt.attempt_number,
        stage=attempt.stage,
        status=attempt.status,
        runner_id=attempt.runner_id,
        stdout_log=attempt.stdout_log,
        stderr_log=attempt.stderr_log,
    )


def _event_view(event: JobEvent) -> JobEventView:
    return JobEventView(
        id=event.id,
        created_at=event.created_at,
        event_type=event.event_type,
        actor=event.actor,
    )
