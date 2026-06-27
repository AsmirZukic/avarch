from __future__ import annotations

from datetime import datetime

from sqlmodel import Session, col, select

from avarch.adapters.job_preparation import (
    JobPreparationError,
    StaleJobProfileError,
    load_job_plan,
    output_exists,
    require_profile,
    source_media_file,
    status_value,
    verify_job_profile,
)
from avarch.adapters.sqlite import job_control
from avarch.adapters.sqlite.job_transitions import require_job
from avarch.adapters.sqlite.models import Job, MediaFileStatus
from avarch.adapters.sqlite.probes import get_canonical_probe_result
from avarch.adapters.sqlite.promotions import has_completed_promotion
from avarch.adapters.sqlite.queue import QueueSelectionError, select_queue_jobs
from avarch.adapters.sqlite.validations import latest_validation
from avarch.application.queue_control import QueueControlError, QueueJobSnapshot
from avarch.application.queue_identity import planning_identity
from avarch.config import AppConfig
from avarch.domain.jobs import JobEventType, JobStage, JobStatus
from avarch.domain.scheduler import RetryFacts


class SqliteQueueControlStore:
    def __init__(self, session: Session) -> None:
        self._session = session

    def has_running_cancel_requests(self) -> bool:
        return (
            self._session.exec(
                select(Job)
                .where(
                    Job.status == JobStatus.ENCODING,
                    col(Job.cancel_requested_at).is_not(None),
                )
                .limit(1)
            ).first()
            is not None
        )

    def select_queue_jobs(
        self,
        *,
        job_ids: set[int] | None,
        statuses: set[JobStatus] | None,
        stages: set[JobStage] | None,
        profile: str | None,
        all_jobs: bool,
    ) -> list[QueueJobSnapshot]:
        try:
            rows = select_queue_jobs(
                self._session,
                job_ids=job_ids,
                statuses=statuses,
                stages=stages,
                profile=profile,
                all_jobs=all_jobs,
            )
        except QueueSelectionError as exc:
            raise QueueControlError(str(exc)) from exc
        return [
            QueueJobSnapshot(
                job_id=_require_id(row),
                status=JobStatus(row.status),
                stage=JobStage(row.stage),
            )
            for row in rows
        ]

    def cancel_job(
        self,
        *,
        job_id: int,
        actor: str,
        now: datetime,
        reason: str,
        event_type: JobEventType,
        details_json: str,
    ) -> None:
        try:
            job_control.cancel_job(
                self._session,
                job_id=job_id,
                actor=actor,
                now=now,
                reason=reason,
                event_type=event_type,
                details_json=details_json,
            )
        except job_control.JobControlError as exc:
            raise QueueControlError(str(exc)) from exc


class SqliteQueueRetryStore(SqliteQueueControlStore):
    def __init__(self, session: Session, *, config: AppConfig) -> None:
        super().__init__(session)
        self._config = config

    def retry_facts(self, *, job_id: int) -> RetryFacts:
        job = require_job(self._session, job_id)
        try:
            media_file = source_media_file(self._session, job.media_file_id)
        except JobPreparationError as exc:
            raise QueueControlError("Source file is missing; scan and enqueue new work.") from exc
        if status_value(media_file.status) == MediaFileStatus.MISSING.value:
            raise QueueControlError("Source file is missing; scan and enqueue new work.")
        if media_file.fs_fingerprint != job.source_fs_fingerprint:
            raise QueueControlError("Source identity changed; scan and enqueue new work.")
        try:
            verify_job_profile(self._config, job)
        except StaleJobProfileError as exc:
            raise QueueControlError(str(exc)) from exc
        identity = planning_identity(require_profile(self._config, job.profile_name))

        canonical_probe = get_canonical_probe_result(self._session, media_file)
        canonical_probe_matches = (
            canonical_probe is not None and canonical_probe.probe_hash == job.probe_hash
        )
        plan_artifact_valid = False
        plan_identity_matches = False
        if canonical_probe_matches and job.plan_path is not None and job.plan_hash is not None:
            try:
                plan = load_job_plan(job)
            except JobPreparationError:
                pass
            else:
                plan_artifact_valid = True
                plan_identity_matches = (
                    plan.profile_hash == identity.profile_hash
                    and plan.vapoursynth.identity_hash == identity.vapoursynth_identity_hash
                    and plan.execution_identity.identity_hash == identity.execution_identity_hash
                )
        validation = latest_validation(self._session, job)
        return RetryFacts(
            canonical_probe_matches=canonical_probe_matches,
            plan_artifact_valid=plan_artifact_valid,
            plan_identity_matches=plan_identity_matches,
            output_exists=output_exists(job),
            validation_passed=validation is not None and validation.passed,
            promotion_completed=has_completed_promotion(self._session, job),
        )

    def request_job_retry(
        self,
        *,
        job_id: int,
        next_stage: JobStage,
        actor: str,
        now: datetime,
    ) -> None:
        job = require_job(self._session, job_id)
        try:
            job_control.request_job_retry(
                self._session,
                job=job,
                next_stage=next_stage,
                now=now,
                actor=actor,
            )
        except job_control.JobControlError as exc:
            raise QueueControlError(str(exc)) from exc


def _require_id(value: object) -> int:
    identifier = getattr(value, "id", None)
    if not isinstance(identifier, int):
        raise QueueControlError("Expected a persisted row id.")
    return identifier
