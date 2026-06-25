from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlmodel import Session

from avarch.adapters.sqlite import job_control
from avarch.adapters.sqlite.job_transitions import require_job
from avarch.adapters.sqlite.models import Job, MediaFileStatus
from avarch.adapters.sqlite.probes import get_canonical_probe_result
from avarch.adapters.sqlite.promotions import has_completed_promotion
from avarch.adapters.sqlite.queue import (
    QueueSelectionError,
    create_queue_job,
    enqueue_candidate_media_files,
    failed_jobs_for_retry,
    find_existing_queue_job,
    select_queue_jobs,
)
from avarch.adapters.sqlite.validations import latest_validation
from avarch.config import AppConfig
from avarch.domain.jobs import JobStage, JobStatus, job_can_retry
from avarch.domain.scheduler import (
    RetryFacts,
    select_retry_stage,
)
from avarch.scheduler_support import (
    JobPreparationError,
    StaleJobProfileError,
    build_queue_key,
    load_job_plan,
    output_exists,
    planning_identity,
    require_profile,
    source_media_file,
    status_value,
    verify_job_profile,
)


@dataclass(frozen=True, slots=True)
class EnqueueSummary:
    selected: int
    created: int
    existing: int
    missing_skipped: int


@dataclass(frozen=True, slots=True)
class RetrySummary:
    eligible: int
    reset_to_probe: int
    reset_to_plan: int
    reset_to_encode: int
    reset_to_validate: int
    requires_requeue: int


@dataclass(frozen=True, slots=True)
class QueueRetrySummary:
    matched: int
    retryable: int
    requires_requeue: int
    reset_to_probe: int
    reset_to_plan: int
    reset_to_encode: int
    reset_to_validate: int
    return_to_promote: int


def enqueue_inventory(
    session: Session,
    *,
    config: AppConfig,
    profile_name: str,
    priority: int,
    now: datetime,
    media_file_ids: tuple[int, ...] | None = None,
) -> EnqueueSummary:
    resolved_profile = require_profile(config, profile_name)
    identity = planning_identity(resolved_profile)
    media_files = enqueue_candidate_media_files(session, media_file_ids=media_file_ids)
    selected = 0
    created = 0
    existing = 0
    missing_skipped = 0

    for media_file in media_files:
        if status_value(media_file.status) == MediaFileStatus.MISSING.value:
            missing_skipped += 1
            continue
        selected += 1
        canonical_probe = get_canonical_probe_result(session, media_file)
        probe_hash = canonical_probe.probe_hash if canonical_probe is not None else None
        queue_key = build_queue_key(
            media_path=Path(media_file.path),
            source_fs_fingerprint=media_file.fs_fingerprint,
            profile_name=resolved_profile.name,
            profile_hash=identity.profile_hash,
            probe_hash=probe_hash,
            vapoursynth_identity_hash=identity.vapoursynth_identity_hash,
            execution_identity_hash=identity.execution_identity_hash,
        )
        if find_existing_queue_job(session, queue_key=queue_key) is not None:
            existing += 1
            continue

        create_queue_job(
            session,
            media_file=media_file,
            profile_name=resolved_profile.name,
            profile_hash=identity.profile_hash,
            queue_key=queue_key,
            probe_result_id=canonical_probe.id if canonical_probe is not None else None,
            probe_hash=probe_hash,
            priority=priority,
            now=now,
        )
        created += 1

    return EnqueueSummary(
        selected=selected,
        created=created,
        existing=existing,
        missing_skipped=missing_skipped,
    )


def retry_job(
    session: Session,
    *,
    job_id: int,
    config: AppConfig,
    actor: str,
    now: datetime,
) -> JobStage:
    job = require_job(session, job_id)
    if not job_can_retry(job.status):
        raise job_control.JobControlError(f"Job {job_id} cannot be retried from {job.status}.")
    next_stage = _resolve_retry_stage(session, job, config=config)
    if next_stage is None:
        raise job_control.JobControlError("Job already has a completed promotion.")
    job_control.request_job_retry(
        session,
        job=job,
        next_stage=next_stage,
        now=now,
        actor=actor,
    )
    return next_stage


def retry_failed_jobs(
    session: Session,
    *,
    config: AppConfig,
    now: datetime,
) -> RetrySummary:
    jobs = failed_jobs_for_retry(session)
    eligible = 0
    reset_to_probe = 0
    reset_to_plan = 0
    reset_to_encode = 0
    reset_to_validate = 0
    requires_requeue = 0
    for job in jobs:
        try:
            next_stage = _resolve_retry_stage(session, job, config=config)
        except job_control.JobControlError:
            requires_requeue += 1
            continue
        if next_stage is None:
            requires_requeue += 1
            continue
        eligible += 1
        if next_stage == JobStage.PROBE:
            reset_to_probe += 1
        elif next_stage == JobStage.PLAN:
            reset_to_plan += 1
        elif next_stage == JobStage.VALIDATE:
            reset_to_validate += 1
        else:
            reset_to_encode += 1
        job_control.reset_retry_job(session, job=job, next_stage=next_stage, now=now)
    return RetrySummary(
        eligible=eligible,
        reset_to_probe=reset_to_probe,
        reset_to_plan=reset_to_plan,
        reset_to_encode=reset_to_encode,
        reset_to_validate=reset_to_validate,
        requires_requeue=requires_requeue,
    )


def retry_queue(
    session: Session,
    *,
    config: AppConfig,
    actor: str,
    now: datetime,
    job_ids: set[int] | None = None,
    statuses: set[JobStatus] | None = None,
    stages: set[JobStage] | None = None,
    profile: str | None = None,
    all_jobs: bool = False,
    confirm: bool = False,
) -> QueueRetrySummary:
    try:
        jobs = select_queue_jobs(
            session,
            job_ids=job_ids,
            statuses=statuses,
            stages=stages,
            profile=profile,
            all_jobs=all_jobs,
        )
    except QueueSelectionError as exc:
        raise job_control.JobControlError(str(exc)) from exc
    retryable = 0
    requires_requeue = 0
    reset_to_probe = 0
    reset_to_plan = 0
    reset_to_encode = 0
    reset_to_validate = 0
    return_to_promote = 0
    for job in jobs:
        if not job_can_retry(job.status):
            continue
        try:
            next_stage = _resolve_retry_stage(session, job, config=config)
        except job_control.JobControlError:
            requires_requeue += 1
            continue
        if next_stage is None:
            requires_requeue += 1
            continue
        retryable += 1
        if next_stage == JobStage.PROBE:
            reset_to_probe += 1
        elif next_stage == JobStage.PLAN:
            reset_to_plan += 1
        elif next_stage == JobStage.ENCODE:
            reset_to_encode += 1
        elif next_stage == JobStage.VALIDATE:
            reset_to_validate += 1
        elif next_stage == JobStage.PROMOTE:
            return_to_promote += 1
        if confirm:
            job_control.request_job_retry(
                session,
                job=job,
                next_stage=next_stage,
                now=now,
                actor=actor,
            )
    return QueueRetrySummary(
        matched=len(jobs),
        retryable=retryable,
        requires_requeue=requires_requeue,
        reset_to_probe=reset_to_probe,
        reset_to_plan=reset_to_plan,
        reset_to_encode=reset_to_encode,
        reset_to_validate=reset_to_validate,
        return_to_promote=return_to_promote,
    )


def _resolve_retry_stage(
    session: Session,
    job: Job,
    *,
    config: AppConfig,
) -> JobStage | None:
    try:
        media_file = source_media_file(session, job.media_file_id)
    except JobPreparationError as exc:
        raise job_control.JobControlError(
            "Source file is missing; scan and enqueue new work."
        ) from exc
    if status_value(media_file.status) == MediaFileStatus.MISSING.value:
        raise job_control.JobControlError("Source file is missing; scan and enqueue new work.")
    if media_file.fs_fingerprint != job.source_fs_fingerprint:
        raise job_control.JobControlError("Source identity changed; scan and enqueue new work.")
    try:
        verify_job_profile(config, job)
    except StaleJobProfileError as exc:
        raise job_control.JobControlError(str(exc)) from exc
    identity = planning_identity(require_profile(config, job.profile_name))

    canonical_probe = get_canonical_probe_result(session, media_file)
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
    validation = latest_validation(session, job)
    return select_retry_stage(
        RetryFacts(
            canonical_probe_matches=canonical_probe_matches,
            plan_artifact_valid=plan_artifact_valid,
            plan_identity_matches=plan_identity_matches,
            output_exists=output_exists(job),
            validation_passed=validation is not None and validation.passed,
            promotion_completed=has_completed_promotion(session, job),
        )
    )
