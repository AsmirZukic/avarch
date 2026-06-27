from __future__ import annotations

from datetime import datetime

from sqlmodel import Session, col, select

from avarch.adapters.sqlite.models import Job
from avarch.adapters.sqlite.validations import prepare_manual_validation
from avarch.application.manual_validation import ManualValidationPreparationView


class SqliteManualValidationPreparationStore:
    def __init__(self, session: Session) -> None:
        self._session = session

    def prepare_validation_for_job(
        self,
        *,
        job_id: int,
        now: datetime,
    ) -> ManualValidationPreparationView | None:
        job = self._session.get(Job, job_id)
        if job is None or job.id is None:
            return None
        return self._prepare(job, now=now)

    def prepare_validation_for_output(
        self,
        *,
        media_file_id: int,
        output_path: str,
        now: datetime,
    ) -> ManualValidationPreparationView | None:
        jobs = list(
            self._session.exec(
                select(Job).where(
                    Job.media_file_id == media_file_id,
                    Job.output_path == output_path,
                    col(Job.plan_hash).is_not(None),
                )
            ).all()
        )
        if len(jobs) != 1:
            return None
        job = jobs[0]
        if job.id is None:
            return None
        return self._prepare(job, now=now)

    def _prepare(
        self,
        job: Job,
        *,
        now: datetime,
    ) -> ManualValidationPreparationView:
        if job.id is None:
            raise RuntimeError("Manual validation requires a persisted job.")
        preparation = prepare_manual_validation(self._session, job=job, now=now)
        return ManualValidationPreparationView(
            action=preparation.action,
            job_id=job.id,
            plan_path=job.plan_path,
            existing_validation_details_json=(
                preparation.existing_validation.details_json
                if preparation.existing_validation is not None
                else None
            ),
        )
