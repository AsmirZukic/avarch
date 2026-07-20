from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from sqlmodel import Session, col, select

from avarch.adapters.sqlite.models import (
    Job,
    JobAttempt,
    JobEvent,
    MediaFile,
    PerformanceObservation,
)
from avarch.adapters.sqlite.progress import SqliteProgressStore
from avarch.application.job_views import (
    CurrentJobProgressView,
    JobAttemptView,
    JobDetails,
    JobEventView,
    JobListItem,
    PerformanceObservationView,
    RecentFailedJob,
    ResourceDecisionView,
    WorkflowJobItem,
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
        observations_by_attempt = self._observations_by_attempt(
            attempt_ids=tuple(attempt.id for attempt in attempts if attempt.id is not None)
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
            attempt_history=[
                _attempt_view(
                    attempt,
                    observations_by_attempt.get(attempt.id) if attempt.id is not None else None,
                )
                for attempt in attempts
            ],
            events=[_event_view(event) for event in events],
        )

    def workflow_jobs_for_plan_hashes(
        self,
        *,
        plan_hashes: tuple[str, ...],
    ) -> list[WorkflowJobItem]:
        if not plan_hashes:
            return []
        jobs = list(
            self._session.exec(
                select(Job)
                .where(col(Job.plan_hash).in_(plan_hashes))
                .order_by(col(Job.created_at).asc(), col(Job.id).asc())
            ).all()
        )
        return [
            WorkflowJobItem(
                id=job.id,
                status=JobStatus(job.status),
                stage=JobStage(job.stage),
                output_path=job.output_path,
                plan_hash=job.plan_hash,
            )
            for job in jobs
        ]

    def recent_failed_jobs(self, *, limit: int) -> list[RecentFailedJob]:
        failed_jobs = list(
            self._session.exec(
                select(Job)
                .where(Job.status == JobStatus.FAILED)
                .order_by(col(Job.id).desc())
                .limit(limit)
            ).all()
        )
        media_by_id = self._media_by_id()
        attempts_by_job = self._latest_attempts_by_job(
            job_ids=tuple(job.id for job in failed_jobs if job.id is not None)
        )
        return [
            RecentFailedJob(
                id=job.id,
                stage=JobStage(job.stage),
                file_name=_file_name(media_by_id.get(job.media_file_id)),
                last_error_type=job.last_error_type,
                last_error_message=job.last_error_message,
                latest_attempt=attempts_by_job.get(job.id)
                if job.id is not None
                else None,
            )
            for job in failed_jobs
        ]

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
        if attempt is None:
            return None
        observation = self._observation_for_attempt(attempt_id=attempt.id)
        return _attempt_view(attempt, observation)

    def current_job_progress(self, *, job_id: int) -> CurrentJobProgressView | None:
        job = self._session.get(Job, job_id)
        if job is None:
            return None
        attempt = self._latest_attempt_model(job_id=job_id)
        progress = None
        if attempt is not None and attempt.id is not None:
            progress = SqliteProgressStore(self._session).get_snapshot(attempt_id=attempt.id)
        observation = (
            self._observation_for_attempt(attempt_id=attempt.id)
            if attempt is not None
            else None
        )
        return CurrentJobProgressView(
            job_id=job_id,
            job_status=JobStatus(job.status),
            job_stage=JobStage(job.stage),
            attempt_id=attempt.id if attempt is not None else None,
            attempt=_attempt_view(attempt, observation) if attempt is not None else None,
            progress=progress,
        )

    def _media_by_id(self) -> dict[int, MediaFile]:
        return {
            media_file.id: media_file
            for media_file in self._session.exec(select(MediaFile)).all()
            if media_file.id is not None
        }

    def _latest_attempts_by_job(self, *, job_ids: tuple[int, ...]) -> dict[int, JobAttemptView]:
        attempts_by_job: dict[int, JobAttemptView] = {}
        if not job_ids:
            return attempts_by_job
        attempts = list(
            self._session.exec(
                select(JobAttempt)
                .where(col(JobAttempt.job_id).in_(job_ids))
                .order_by(col(JobAttempt.attempt_number).desc())
            ).all()
        )
        observations_by_attempt = self._observations_by_attempt(
            attempt_ids=tuple(attempt.id for attempt in attempts if attempt.id is not None)
        )
        for attempt in attempts:
            observation = (
                observations_by_attempt.get(attempt.id) if attempt.id is not None else None
            )
            attempts_by_job.setdefault(attempt.job_id, _attempt_view(attempt, observation))
        return attempts_by_job

    def _latest_attempt_model(self, *, job_id: int) -> JobAttempt | None:
        return self._session.exec(
            select(JobAttempt)
            .where(JobAttempt.job_id == job_id)
            .order_by(col(JobAttempt.attempt_number).desc())
        ).first()

    def _observations_by_attempt(
        self,
        *,
        attempt_ids: tuple[int, ...],
    ) -> dict[int, PerformanceObservation]:
        if not attempt_ids:
            return {}
        observations = self._session.exec(
            select(PerformanceObservation).where(
                col(PerformanceObservation.attempt_id).in_(attempt_ids)
            )
        ).all()
        return {observation.attempt_id: observation for observation in observations}

    def _observation_for_attempt(
        self,
        *,
        attempt_id: int | None,
    ) -> PerformanceObservation | None:
        if attempt_id is None:
            return None
        return self._session.exec(
            select(PerformanceObservation).where(PerformanceObservation.attempt_id == attempt_id)
        ).first()


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


def _file_name(media_file: MediaFile | None) -> str:
    return Path(media_file.path).name if media_file is not None else "<missing>"


def _attempt_view(
    attempt: JobAttempt,
    observation: PerformanceObservation | None = None,
) -> JobAttemptView:
    return JobAttemptView(
        attempt_number=attempt.attempt_number,
        stage=attempt.stage,
        status=attempt.status,
        runner_id=attempt.runner_id,
        stdout_log=attempt.stdout_log,
        stderr_log=attempt.stderr_log,
        resource_decision=_resource_decision_view(attempt.command_json),
        performance=_performance_observation_view(observation),
    )


def _performance_observation_view(
    observation: PerformanceObservation | None,
) -> PerformanceObservationView | None:
    if observation is None:
        return None
    return PerformanceObservationView(
        schema_version=observation.schema_version,
        total_frames=observation.total_frames,
        observation_duration_seconds=observation.observation_duration_seconds,
        aggregate_fps=observation.aggregate_fps,
        peak_rss_bytes=observation.peak_rss_bytes,
        peak_cgroup_memory_bytes=observation.peak_cgroup_memory_bytes,
        average_cpu_utilization_percent=observation.average_cpu_utilization_percent,
        cpu_throttled_events_delta=observation.cpu_throttled_events_delta,
        cpu_throttled_usec_delta=observation.cpu_throttled_usec_delta,
        memory_oom_events_delta=observation.memory_oom_events_delta,
        memory_oom_kill_events_delta=observation.memory_oom_kill_events_delta,
        incomplete=observation.incomplete,
        resource_policy_hash=observation.resource_policy_hash,
    )


def _resource_decision_view(command_json: str | None) -> ResourceDecisionView | None:
    if command_json is None:
        return None
    try:
        payload: object = json.loads(command_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    payload_dict = cast(dict[str, object], payload)
    decision = payload_dict.get("resource_decision")
    if not isinstance(decision, dict):
        return None
    decision_dict = cast(dict[str, object], decision)
    try:
        confidence = _float_field(decision_dict, "confidence")
        algorithm_version = _int_field(decision_dict, "algorithm_version")
        fallback = _bool_field(decision_dict, "fallback")
        return ResourceDecisionView(
            mode=str(decision_dict["mode"]),
            effective_workers=str(decision_dict["effective_workers"]),
            effective_svt_lp=str(decision_dict["effective_svt_lp"]),
            reason=str(decision_dict["reason"]),
            confidence=confidence,
            algorithm_version=algorithm_version,
            fallback=fallback,
            evidence_count=_optional_int_field(decision_dict, "evidence_count"),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _float_field(values: dict[str, object], key: str) -> float:
    value = values[key]
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        raise TypeError(key)
    return float(value)


def _optional_int_field(values: dict[str, object], key: str) -> int | None:
    value = values.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(key)
    return value


def _int_field(values: dict[str, object], key: str) -> int:
    value = values[key]
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise TypeError(key)
    return int(value)


def _bool_field(values: dict[str, object], key: str) -> bool:
    value = values[key]
    if not isinstance(value, bool):
        raise TypeError(key)
    return value


def _event_view(event: JobEvent) -> JobEventView:
    return JobEventView(
        id=event.id,
        created_at=event.created_at,
        event_type=event.event_type,
        actor=event.actor,
    )
