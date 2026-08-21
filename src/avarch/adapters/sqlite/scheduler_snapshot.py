from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import func
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import Session, col, select

from avarch.adapters.sqlite.models import (
    Job,
    JobAttempt,
    JobAttemptProgress,
    JobEvent,
    MediaFile,
    MediaFileStatus,
    SchedulerSession,
    SchedulerState,
)
from avarch.adapters.sqlite.queue import claimable_jobs
from avarch.adapters.sqlite.scheduler_state import lease_active
from avarch.application.scheduler_blockers import JobEligibilityReason
from avarch.application.scheduler_snapshot import (
    ActiveJobSummary,
    AttemptProgressSummary,
    BlockedJobSummary,
    CapacitySummary,
    EncodeResourceDecisionSummary,
    LifecycleEventSummary,
    PipelineSummary,
    SchedulerAlert,
    SchedulerAlertSeverity,
    SchedulerRuntimeState,
    SchedulerRuntimeSummary,
    SchedulerSessionRunSummary,
    SchedulerSnapshot,
    SessionSummary,
    UpcomingJobSummary,
    WorkspaceSummary,
    project_workflow_steps,
)
from avarch.domain.jobs import AttemptStatus, JobEventType, JobStage, JobStatus
from avarch.domain.progress import ProgressPhase, ProgressUnit
from avarch.domain.scheduler import SchedulerMode

_ACTIVE_STATUSES = {
    JobStatus.ENCODING,
    JobStatus.VALIDATING,
    JobStatus.PROMOTING,
    JobStatus.CLEANING,
}
PROGRESS_STALE_AFTER_SECONDS = 10


class SqliteSchedulerSnapshotQuery:
    def __init__(
        self,
        session: Session,
        *,
        workspace_root: str,
        database_url: str | None = None,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._workspace_root = workspace_root
        self._database_url = database_url
        self._now = now or (lambda: datetime.now(UTC))

    def snapshot(self) -> SchedulerSnapshot:
        captured_at = self._now()
        return SchedulerSnapshot(
            captured_at=captured_at,
            workspace=WorkspaceSummary(
                root_path=self._workspace_root,
                database_url=self._database_url,
            ),
            scheduler=self._scheduler_summary(now=captured_at),
            pipeline=self._pipeline_summary(),
            session=self._session_summary(),
            active_jobs=self._active_jobs(captured_at=captured_at),
            capacity=self._capacity_summary(now=captured_at),
            upcoming_jobs=self._upcoming_jobs(),
            blocked_jobs=self._blocked_jobs(),
            recent_events=self._recent_events(),
            alerts=self._alerts(now=captured_at),
            resources=None,
            forecast=None,
        )

    def _scheduler_summary(self, *, now: datetime) -> SchedulerRuntimeSummary:
        state = self._session.get(SchedulerState, 1)
        if state is None:
            return SchedulerRuntimeSummary(state=SchedulerRuntimeState.STOPPED)

        mode = SchedulerMode(state.mode)
        if state.runner_id is None:
            if mode == SchedulerMode.PAUSED:
                return SchedulerRuntimeSummary(state=SchedulerRuntimeState.PAUSED, mode=mode)
            if mode == SchedulerMode.DRAINING:
                return SchedulerRuntimeSummary(state=SchedulerRuntimeState.DRAINING, mode=mode)
            if mode == SchedulerMode.STOPPING:
                return SchedulerRuntimeSummary(state=SchedulerRuntimeState.STOPPING, mode=mode)
            return SchedulerRuntimeSummary(state=SchedulerRuntimeState.STOPPED, mode=mode)

        stale = not lease_active(state, now=now)
        runtime_state = SchedulerRuntimeState.STALE if stale else SchedulerRuntimeState(mode.value)
        return SchedulerRuntimeSummary(
            state=runtime_state,
            mode=mode,
            owner_id=state.runner_id,
            heartbeat_at=state.heartbeat_at,
            stale=stale,
        )

    def _pipeline_summary(self) -> PipelineSummary:
        counts = {status: 0 for status in JobStatus}
        rows = self._session.exec(select(Job.status, func.count()).group_by(Job.status)).all()
        for status, count in rows:
            counts[JobStatus(status)] = int(count)

        return PipelineSummary(
            queued=counts[JobStatus.QUEUED],
            active=sum(counts[status] for status in _ACTIVE_STATUSES),
            encoded=counts[JobStatus.ENCODED],
            validating=counts[JobStatus.VALIDATING],
            ready_to_promote=counts[JobStatus.READY_TO_PROMOTE],
            promoting=counts[JobStatus.PROMOTING],
            cleaning=counts[JobStatus.CLEANING],
            completed=counts[JobStatus.PROMOTED],
            failed=counts[JobStatus.FAILED],
            validation_failed=counts[JobStatus.VALIDATION_FAILED],
            size_rejected=counts[JobStatus.SIZE_REJECTED],
            cancelled=counts[JobStatus.CANCELLED],
            held=counts[JobStatus.HELD],
        )

    def _capacity_summary(self, *, now: datetime) -> CapacitySummary:
        state = self._session.get(SchedulerState, 1)
        if state is None or state.capacity_cheap_workers is None:
            return CapacitySummary(cheap_workers=0, av1an_jobs=0, file_ops=0)
        stale = state.runner_id is not None and not lease_active(state, now=now)
        return CapacitySummary(
            cheap_workers=state.capacity_cheap_workers,
            cheap_active=state.capacity_cheap_active or 0,
            av1an_jobs=state.capacity_av1an_jobs or 0,
            av1an_active=state.capacity_av1an_active or 0,
            file_ops=state.capacity_file_ops or 0,
            file_ops_active=state.capacity_file_ops_active or 0,
            av1an_workers_configured=self._active_av1an_workers_configured(),
            observed_at=state.capacity_observed_at,
            stale=stale,
        )

    def _active_av1an_workers_configured(self) -> int | None:
        rows = self._session.exec(
            select(JobAttempt.command_json)
            .where(col(JobAttempt.stage).in_([JobStage.SCENE_DETECT, JobStage.ENCODE]))
            .where(JobAttempt.status == AttemptStatus.RUNNING)
        ).all()
        workers = tuple(
            worker_count
            for command_json in rows
            if (worker_count := _av1an_workers_from_command(command_json)) is not None
        )
        if not workers:
            return None
        return sum(workers)

    def _active_jobs(self, *, captured_at: datetime) -> tuple[ActiveJobSummary, ...]:
        rows = list(
            self._session.exec(
                select(Job, MediaFile)
                .join(MediaFile, cast(ColumnElement[bool], MediaFile.id == Job.media_file_id))
                .where(col(Job.status).in_(_ACTIVE_STATUSES))
                .order_by(col(Job.priority).desc(), col(Job.created_at).asc(), col(Job.id).asc())
            ).all()
        )
        job_ids = tuple(job.id for job, _media in rows if job.id is not None)
        attempts_by_job = self._latest_attempts_by_job(job_ids)
        progress_by_attempt = self._progress_by_attempt(
            tuple(
                attempt.id
                for attempt in attempts_by_job.values()
                if attempt.id is not None
            )
        )
        return tuple(
            _active_job_summary(
                job=job,
                media_file=media_file,
                attempt=attempts_by_job.get(job.id),
                progress=progress_by_attempt.get(_required_id(attempts_by_job[job.id].id))
                if job.id in attempts_by_job and attempts_by_job[job.id].id is not None
                else None,
                captured_at=captured_at,
            )
            for job, media_file in rows
            if job.id is not None
        )

    def _upcoming_jobs(self) -> tuple[UpcomingJobSummary, ...]:
        selected = self._selected_upcoming_jobs(limit=3)
        media_ids = tuple(job.media_file_id for job in selected)
        media_by_id = {
            media_file.id: media_file
            for media_file in self._session.exec(
                select(MediaFile).where(col(MediaFile.id).in_(media_ids))
            ).all()
            if media_file.id is not None
        }
        return tuple(
            UpcomingJobSummary(
                job_id=_required_id(job.id),
                source_path=media_by_id[job.media_file_id].path,
                profile_name=job.profile_name,
                stage=JobStage(job.stage),
                status=JobStatus(job.status),
                priority=job.priority,
                queued_at=job.created_at,
                selection_position=index,
                selection_confidence="current_snapshot",
            )
            for index, job in enumerate(selected, start=1)
            if job.media_file_id in media_by_id
        )

    def _selected_upcoming_jobs(self, *, limit: int) -> list[Job]:
        active_job_ids = {
            _required_id(job.id)
            for job in self._session.exec(
                select(Job).where(col(Job.status).in_(_ACTIVE_STATUSES))
            ).all()
            if job.id is not None
        }
        return claimable_jobs(session=self._session, active_job_ids=active_job_ids)[:limit]

    def _session_summary(self) -> SessionSummary | None:
        sessions = tuple(
            self._session.exec(
                select(SchedulerSession)
                .order_by(
                    col(SchedulerSession.started_at).desc(),
                    col(SchedulerSession.id).desc(),
                )
                .limit(5)
            ).all()
        )
        if not sessions:
            return None
        current = next((session for session in sessions if session.ended_at is None), None)
        return SessionSummary(
            current=_session_run_summary(current) if current is not None else None,
            recent=tuple(_session_run_summary(session) for session in sessions),
        )

    def _recent_events(self) -> tuple[LifecycleEventSummary, ...]:
        events = self._session.exec(
            select(JobEvent)
            .order_by(col(JobEvent.created_at).desc(), col(JobEvent.id).desc())
            .limit(8)
        ).all()
        return tuple(_lifecycle_event_summary(event) for event in events)

    def _blocked_jobs(self) -> tuple[BlockedJobSummary, ...]:
        rows = self._session.exec(
            select(Job, MediaFile)
            .join(MediaFile, cast(ColumnElement[bool], MediaFile.id == Job.media_file_id))
            .where(
                col(Job.status).not_in(
                    [JobStatus.PROMOTED, JobStatus.SKIPPED, JobStatus.CANCELLED]
                )
            )
            .order_by(col(Job.priority).desc(), col(Job.created_at).asc(), col(Job.id).asc())
            .limit(20)
        ).all()
        blocked: list[BlockedJobSummary] = []
        for job, media_file in rows:
            reason = _blocked_reason(job, media_file)
            if reason is None:
                continue
            blocked.append(
                BlockedJobSummary(
                    job_id=_required_id(job.id),
                    source_path=media_file.path,
                    profile_name=job.profile_name,
                    stage=JobStage(job.stage),
                    status=JobStatus(job.status),
                    reason=reason,
                    details=_blocked_details(job, media_file, reason),
                )
            )
        return tuple(blocked[:8])

    def _alerts(self, *, now: datetime) -> tuple[SchedulerAlert, ...]:
        state = self._session.get(SchedulerState, 1)
        if state is None:
            return ()
        mode = SchedulerMode(state.mode)
        alerts: list[SchedulerAlert] = []
        if state.runner_id is not None and not lease_active(state, now=now):
            alerts.append(
                SchedulerAlert(
                    severity=SchedulerAlertSeverity.WARNING,
                    code="stale_scheduler_lease",
                    message="Scheduler lease is stale.",
                    observed_at=now,
                )
            )
        if mode == SchedulerMode.PAUSED:
            alerts.append(
                SchedulerAlert(
                    severity=SchedulerAlertSeverity.INFO,
                    code="scheduler_paused",
                    message="Scheduler is paused.",
                    observed_at=now,
                )
            )
        if mode == SchedulerMode.DRAINING:
            alerts.append(
                SchedulerAlert(
                    severity=SchedulerAlertSeverity.INFO,
                    code="scheduler_draining",
                    message="Scheduler is draining.",
                    observed_at=now,
                )
            )
        return tuple(alerts)

    def _latest_attempts_by_job(self, job_ids: tuple[int, ...]) -> dict[int, JobAttempt]:
        if not job_ids:
            return {}
        attempts = list(
            self._session.exec(
                select(JobAttempt)
                .where(col(JobAttempt.job_id).in_(job_ids))
                .order_by(
                    col(JobAttempt.job_id).asc(),
                    col(JobAttempt.attempt_number).desc(),
                    col(JobAttempt.id).desc(),
                )
            ).all()
        )
        latest: dict[int, JobAttempt] = {}
        for attempt in attempts:
            latest.setdefault(attempt.job_id, attempt)
        return latest

    def _progress_by_attempt(
        self,
        attempt_ids: tuple[int, ...],
    ) -> dict[int, JobAttemptProgress]:
        if not attempt_ids:
            return {}
        return {
            progress.attempt_id: progress
            for progress in self._session.exec(
                select(JobAttemptProgress).where(col(JobAttemptProgress.attempt_id).in_(attempt_ids))
            ).all()
        }


def _active_job_summary(
    *,
    job: Job,
    media_file: MediaFile,
    attempt: JobAttempt | None,
    progress: JobAttemptProgress | None,
    captured_at: datetime,
) -> ActiveJobSummary:
    attempt_status = AttemptStatus(attempt.status) if attempt is not None else None
    job_stage = JobStage(job.stage)
    progress = _progress_for_active_stage(job_stage, progress)
    attempt_progress = (
        _attempt_progress_summary(
            attempt=attempt,
            progress=progress,
            captured_at=captured_at,
            job_stage=job_stage,
        )
        if attempt is not None
        else None
    )
    return ActiveJobSummary(
        job_id=_required_id(job.id),
        source_path=media_file.path,
        profile_name=job.profile_name,
        status=JobStatus(job.status),
        stage=job_stage,
        priority=job.priority,
        queued_at=job.created_at,
        started_at=job.started_at,
        attempt=attempt_progress,
        workflow_steps=project_workflow_steps(
            job_status=JobStatus(job.status),
            job_stage=job_stage,
            attempt_status=attempt_status,
        ),
    )


def _progress_for_active_stage(
    job_stage: JobStage,
    progress: JobAttemptProgress | None,
) -> JobAttemptProgress | None:
    if (
        job_stage == JobStage.ENCODE
        and progress is not None
        and ProgressPhase(progress.phase) == ProgressPhase.SCENE_DETECTION
    ):
        return None
    return progress


def _session_run_summary(session: SchedulerSession) -> SchedulerSessionRunSummary:
    return SchedulerSessionRunSummary(
        session_id=_required_id(session.id),
        owner_id=session.owner_id,
        workspace_id=session.workspace_id,
        pid=session.pid,
        host=session.host,
        started_at=session.started_at,
        ended_at=session.ended_at,
        end_reason=session.end_reason,
        active=session.ended_at is None,
    )


def _lifecycle_event_summary(event: JobEvent) -> LifecycleEventSummary:
    return LifecycleEventSummary(
        event_id=_required_id(event.id),
        job_id=event.job_id,
        attempt_id=event.attempt_id,
        scheduler_session_id=event.scheduler_session_id,
        event_type=JobEventType(event.event_type),
        stage=JobStage(event.stage) if event.stage is not None else None,
        actor=event.actor,
        reason=event.reason,
        details=_event_details(event.details_json),
        created_at=event.created_at,
    )


def _blocked_reason(job: Job, media_file: MediaFile) -> JobEligibilityReason | None:
    if job.hold_requested_at is not None or JobStatus(job.status) == JobStatus.HELD:
        return JobEligibilityReason.JOB_HELD
    if job.cancel_requested_at is not None:
        return JobEligibilityReason.CANCEL_REQUESTED
    if MediaFileStatus(media_file.status) == MediaFileStatus.MISSING:
        return JobEligibilityReason.SOURCE_MISSING
    if (
        JobStage(job.stage)
        in {JobStage.SCENE_DETECT, JobStage.ENCODE, JobStage.VALIDATE, JobStage.PROMOTE}
        and job.plan_path is None
    ):
        return JobEligibilityReason.PLAN_MISSING
    return None


def _blocked_details(
    job: Job,
    media_file: MediaFile,
    reason: JobEligibilityReason,
) -> dict[str, object]:
    if reason == JobEligibilityReason.JOB_HELD:
        return {"hold_reason": job.hold_reason} if job.hold_reason else {}
    if reason == JobEligibilityReason.CANCEL_REQUESTED:
        return {"cancel_reason": job.cancel_reason} if job.cancel_reason else {}
    if reason == JobEligibilityReason.SOURCE_MISSING:
        return {"media_status": MediaFileStatus(media_file.status).value}
    if reason == JobEligibilityReason.PLAN_MISSING:
        return {"stage": JobStage(job.stage).value}
    return {}


def _av1an_workers_from_command(command_json: str | None) -> int | None:
    if command_json is None:
        return None
    try:
        value: object = json.loads(command_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    payload = cast(dict[str, object], value)
    argv = payload.get("av1an_argv")
    if not isinstance(argv, list):
        return None
    args = [str(arg) for arg in cast(list[object], argv)]
    try:
        index = args.index("--workers")
    except ValueError:
        return None
    next_index = index + 1
    if next_index >= len(args):
        return None
    try:
        workers = int(args[next_index])
    except ValueError:
        return None
    return workers if workers > 0 else None


def _attempt_progress_summary(
    *,
    attempt: JobAttempt,
    progress: JobAttemptProgress | None,
    captured_at: datetime,
    job_stage: JobStage | None = None,
) -> AttemptProgressSummary:
    frames_current: int | None = None
    frames_total: int | None = None
    rate_per_second: float | None = None
    speed_ratio: float | None = None
    observed_at: datetime | None = None
    eta_seconds: int | None = None
    last_update_age_seconds: int | None = None
    if progress is not None:
        if progress.unit == ProgressUnit.FRAMES:
            frames_current = _int_or_none(progress.current_value)
            frames_total = _int_or_none(progress.total_value)
        rate_per_second = progress.rate_per_second
        speed_ratio = progress.speed_ratio
        observed_at = progress.observed_at
        last_update_age_seconds = _elapsed_seconds(started_at=progress.observed_at, now=captured_at)
        eta_seconds = _eta_seconds(
            current=progress.current_value,
            total=progress.total_value,
            rate_per_second=progress.rate_per_second,
        )
    return AttemptProgressSummary(
        attempt_id=_required_id(attempt.id),
        attempt_number=attempt.attempt_number,
        status=AttemptStatus(attempt.status),
        phase=_attempt_phase(progress=progress, job_stage=job_stage),
        message=_attempt_progress_message(progress=progress, job_stage=job_stage),
        frames_current=frames_current,
        frames_total=frames_total,
        rate_per_second=rate_per_second,
        speed_ratio=speed_ratio,
        eta_seconds=eta_seconds,
        elapsed_seconds=_elapsed_seconds(started_at=attempt.started_at, now=captured_at),
        observed_at=observed_at,
        chunks_current=progress.chunks_current if progress is not None else None,
        chunks_total=progress.chunks_total if progress is not None else None,
        bitrate_kbps=progress.bitrate_kbps if progress is not None else None,
        estimated_output_bytes=progress.estimated_output_bytes if progress is not None else None,
        written_output_bytes=progress.written_output_bytes if progress is not None else None,
        stale=(
            last_update_age_seconds is not None
            and last_update_age_seconds > PROGRESS_STALE_AFTER_SECONDS
        ),
        last_update_age_seconds=last_update_age_seconds,
        resource_decision=_resource_decision_from_command(attempt.command_json),
    )


def _resource_decision_from_command(
    command_json: str | None,
) -> EncodeResourceDecisionSummary | None:
    payload = _command_payload(command_json)
    if payload is None:
        return None
    decision = payload.get("resource_decision")
    if not isinstance(decision, dict):
        return None
    values = cast(dict[str, object], decision)
    try:
        workers = _positive_int_or_text(values["effective_workers"])
        svt_lp = _positive_int_or_text(values["effective_svt_lp"])
        confidence = _finite_float(values["confidence"])
        algorithm_version = _positive_int(values["algorithm_version"])
        fallback = values["fallback"]
        evidence_count = values.get("evidence_count")
        if not isinstance(fallback, bool):
            return None
        if evidence_count is not None and (
            isinstance(evidence_count, bool)
            or not isinstance(evidence_count, int)
            or evidence_count < 0
        ):
            return None
        return EncodeResourceDecisionSummary(
            mode=str(values["mode"]),
            effective_workers=workers,
            effective_svt_lp=svt_lp,
            reason=str(values["reason"]),
            confidence=confidence,
            algorithm_version=algorithm_version,
            fallback=fallback,
            evidence_count=evidence_count,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _command_payload(command_json: str | None) -> dict[str, object] | None:
    if command_json is None:
        return None
    try:
        value: object = json.loads(command_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    return cast(dict[str, object], value)


def _positive_int_or_text(value: object) -> int | str:
    if isinstance(value, bool):
        raise TypeError
    if isinstance(value, int):
        if value <= 0:
            raise ValueError
        return value
    if isinstance(value, str) and value:
        return value
    raise TypeError


def _positive_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise TypeError
    return value


def _finite_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError
    result = float(value)
    if not 0 <= result <= 1:
        raise ValueError
    return result


def _attempt_phase(
    *,
    progress: JobAttemptProgress | None,
    job_stage: JobStage | None,
) -> ProgressPhase | None:
    if progress is not None:
        return ProgressPhase(progress.phase)
    if job_stage == JobStage.ENCODE:
        return ProgressPhase.ENCODING
    return None


def _attempt_progress_message(
    *,
    progress: JobAttemptProgress | None,
    job_stage: JobStage | None,
) -> str | None:
    if progress is not None:
        return progress.message
    if job_stage == JobStage.ENCODE:
        return "telemetry pending"
    return None


def _eta_seconds(
    *,
    current: float | None,
    total: float | None,
    rate_per_second: float | None,
) -> int | None:
    if current is None or total is None or rate_per_second is None or rate_per_second <= 0:
        return None
    remaining = max(0.0, total - current)
    return int(remaining / rate_per_second)


def _elapsed_seconds(*, started_at: datetime, now: datetime) -> int:
    delta = now.replace(tzinfo=None) - started_at.replace(tzinfo=None)
    return max(0, int(delta.total_seconds()))


def _int_or_none(value: float | None) -> int | None:
    if value is None:
        return None
    return int(value)


def _required_id(value: int | None) -> int:
    if value is None:
        raise ValueError("Expected persisted row id")
    return value


def _event_details(details_json: str | None) -> dict[str, object]:
    if details_json is None:
        return {}
    value: object = json.loads(details_json)
    if isinstance(value, dict):
        details = cast(dict[object, object], value)
        return {str(key): item for key, item in details.items()}
    return {"value": value}
