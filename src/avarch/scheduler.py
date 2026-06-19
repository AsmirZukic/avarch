from __future__ import annotations

import asyncio
import hashlib
import os
import socket
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from sqlmodel import Session, col, select

from avarch.config import AppConfig
from avarch.contracts import QUEUE_CONTRACT
from avarch.db import create_db_engine
from avarch.execution import build_av1an_command, execute_plan, should_resume_av1an
from avarch.models.db import (
    Job,
    JobAttempt,
    JobEvent,
    MediaFile,
    MediaFileStatus,
    ProbeResult,
    SchedulerState,
    ValidationResult,
)
from avarch.models.execution import ExecutionError, ExecutionInterruptedError
from avarch.models.plan import TranscodePlan
from avarch.models.scheduler import (
    AttemptStatus,
    JobEventType,
    JobStage,
    JobStatus,
    ResourceClass,
    SchedulerMode,
)
from avarch.planner import (
    PlanArtifactConflictError,
    PlanningError,
    build_execution_identity,
    build_plan,
    build_profile_hash,
    load_planning_context,
    match_profile,
    write_plan_artifacts,
)
from avarch.probe import (
    ProbeError,
    build_ffprobe_command,
    get_canonical_probe_result,
    normalize_probe,
    run_ffprobe,
    store_probe_result,
)
from avarch.profiles.registry import ProfileRegistry, ResolvedProfile, UnknownProfileError
from avarch.scanner import create_file_snapshot
from avarch.serialization import canonical_json
from avarch.validation import (
    ValidationError as OutputValidationError,
)
from avarch.validation import (
    failed_required_check_names,
    persist_validation_result,
    validate_output,
)
from avarch.vapoursynth import (
    GENERATOR_VERSION,
    VapourSynthGenerationError,
    build_vapoursynth_identity_hash,
    generate_vapoursynth_script,
    resolve_vapoursynth_filter,
    resolve_vapoursynth_template,
    validate_script_syntax,
)

SCHEDULER_LEASE_SECONDS = 30.0
SCHEDULER_HEARTBEAT_SECONDS = 5.0
SCHEDULER_POLL_SECONDS = 1.0
SCHEDULER_IDLE_EXIT_SECONDS = 2.0
SCHEDULER_CONTROL_POLL_SECONDS = 0.5
MAX_CLI_LOG_TAIL_BYTES = 64 * 1024

ProgressCallback = Callable[[int, float | None], None]
JobWorker = Callable[..., Awaitable[Any]]


class SchedulerError(RuntimeError):
    pass


class SchedulerAlreadyRunningError(SchedulerError):
    pass


class SchedulerLeaseLostError(SchedulerError):
    pass


class SchedulerControlError(SchedulerError):
    pass


class JobClaimError(SchedulerError):
    pass


class JobControlError(SchedulerError):
    pass


class StaleJobSourceError(SchedulerError):
    pass


class StaleJobProfileError(SchedulerError):
    pass


class DuplicateQueuedWorkError(SchedulerError):
    pass


class JobPreparationError(SchedulerError):
    pass


@dataclass(frozen=True, slots=True)
class EnqueueSummary:
    selected: int
    created: int
    existing: int
    missing_skipped: int


@dataclass(frozen=True, slots=True)
class RecoverySummary:
    recovered_jobs: int
    interrupted_attempts: int


@dataclass(frozen=True, slots=True)
class RetrySummary:
    eligible: int
    reset_to_probe: int
    reset_to_plan: int
    reset_to_encode: int
    reset_to_validate: int
    requires_requeue: int


@dataclass(frozen=True, slots=True)
class SchedulerRunSummary:
    completed: int
    failed: int
    skipped: int
    idle: bool


@dataclass(frozen=True, slots=True)
class QueueClearSummary:
    matched: int
    immediate_cancel: int
    running_requests: int
    promotion_excluded: int
    completed_excluded: int
    changed: int
    operation_id: str


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


@dataclass(frozen=True, slots=True)
class SchedulerStatus:
    mode: SchedulerMode
    control_generation: int
    acknowledged_generation: int
    runner_id: str | None
    heartbeat_at: datetime | None
    lease_expires_at: datetime | None
    lease_state: str
    counts_by_status: dict[JobStatus, int]
    cancel_pending: int
    hold_pending: int
    active_jobs: list[Job]


def utc_now() -> datetime:
    return datetime.now(UTC)


def cli_actor() -> str:
    return f"cli:{socket.gethostname()}:{os.getpid()}"


def build_queue_key(
    *,
    media_path: Path,
    source_fs_fingerprint: str,
    profile_name: str,
    profile_hash: str,
    probe_hash: str | None,
    vapoursynth_identity_hash: str,
    execution_identity_hash: str,
) -> str:
    payload_json = canonical_json(
        {
            "media_path": str(media_path.resolve()),
            "source_fs_fingerprint": source_fs_fingerprint,
            "profile_name": profile_name,
            "profile_hash": profile_hash,
            "probe_hash": probe_hash,
            "vapoursynth_identity_hash": vapoursynth_identity_hash,
            "execution_identity_hash": execution_identity_hash,
            "queue_contract": QUEUE_CONTRACT,
        }
    )
    payload = f"{QUEUE_CONTRACT}\0".encode() + payload_json.encode("utf-8")
    return hashlib.blake2b(payload, digest_size=32).hexdigest()


def find_existing_queue_job(
    session: Session,
    *,
    queue_key: str,
) -> Job | None:
    return session.exec(select(Job).where(Job.queue_key == queue_key)).first()


def enqueue_inventory(
    session: Session,
    *,
    config: AppConfig,
    profile_name: str,
    priority: int,
    now: datetime,
    media_file_ids: tuple[int, ...] | None = None,
) -> EnqueueSummary:
    resolved_profile = _require_profile(config, profile_name)
    identity = _planning_identity(resolved_profile)
    statement = select(MediaFile).order_by(MediaFile.path)
    if media_file_ids is not None:
        statement = statement.where(col(MediaFile.id).in_(media_file_ids))
    media_files = list(session.exec(statement).all())
    selected = 0
    created = 0
    existing = 0
    missing_skipped = 0

    for media_file in media_files:
        if _status_value(media_file.status) == MediaFileStatus.MISSING.value:
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

        session.add(
            Job(
                media_file_id=_require_id(media_file),
                profile_name=resolved_profile.name,
                profile_hash=identity.profile_hash,
                source_fs_fingerprint=media_file.fs_fingerprint,
                queue_key=queue_key,
                probe_result_id=canonical_probe.id if canonical_probe is not None else None,
                probe_hash=probe_hash,
                status=JobStatus.PENDING,
                stage=JobStage.PLAN if canonical_probe is not None else JobStage.PROBE,
                priority=priority,
                attempts=0,
                created_at=now,
                updated_at=now,
            )
        )
        created += 1

    return EnqueueSummary(
        selected=selected,
        created=created,
        existing=existing,
        missing_skipped=missing_skipped,
    )


def claim_job_stage(
    session: Session,
    *,
    job_id: int,
    runner_id: str,
    now: datetime,
) -> JobAttempt:
    job = _require_job(session, job_id)
    if job.status != JobStatus.PENDING:
        raise JobClaimError(f"Job is not pending: {job_id}")

    job.status = JobStatus.RUNNING
    job.claimed_by = runner_id
    job.attempts += 1
    job.started_at = job.started_at or now
    job.updated_at = now
    attempt = JobAttempt(
        job_id=job_id,
        attempt_number=job.attempts,
        stage=job.stage,
        resource_class=resource_for_stage(job.stage),
        status=AttemptStatus.RUNNING,
        runner_id=runner_id,
        started_at=now,
    )
    session.add(job)
    session.add(attempt)
    session.flush()
    if attempt.id is None:
        raise JobClaimError("Attempt id was not assigned after claim.")
    return attempt


def complete_job_stage(
    session: Session,
    *,
    job_id: int,
    attempt_id: int,
    next_stage: JobStage | None,
    now: datetime,
) -> None:
    job = _require_job(session, job_id)
    attempt = _require_attempt(session, attempt_id)
    if job.cancel_requested_at is not None:
        _cancel_claimed_job(session, job=job, attempt=attempt, now=now)
        return

    attempt.status = AttemptStatus.COMPLETED
    attempt.finished_at = now
    job.last_error_type = None
    job.last_error_message = None
    job.claimed_by = None
    job.updated_at = now
    if next_stage is None:
        job.status = JobStatus.COMPLETED
        job.finished_at = now
    else:
        job.status = JobStatus.HELD if job.hold_requested_at is not None else JobStatus.PENDING
        job.stage = next_stage
        job.finished_at = None
        if job.status == JobStatus.HELD:
            job.held_at = now
            _add_job_event(
                session,
                job_id=job_id,
                event_type=JobEventType.HELD,
                actor=job.hold_requested_by or "scheduler",
                reason=job.hold_reason,
                now=now,
            )
    session.add(job)
    session.add(attempt)


def fail_job_stage(
    session: Session,
    *,
    job_id: int,
    attempt_id: int,
    error: Exception,
    exit_code: int | None,
    now: datetime,
) -> None:
    job = _require_job(session, job_id)
    attempt = _require_attempt(session, attempt_id)
    if job.cancel_requested_at is not None:
        _cancel_claimed_job(session, job=job, attempt=attempt, now=now)
        return

    attempt.status = AttemptStatus.FAILED
    attempt.error_type = error.__class__.__name__
    attempt.error_message = str(error)
    attempt.exit_code = exit_code
    attempt.finished_at = now
    job.status = JobStatus.FAILED
    job.claimed_by = None
    job.last_error_type = attempt.error_type
    job.last_error_message = attempt.error_message
    _clear_hold_fields(job)
    job.updated_at = now
    job.finished_at = now
    session.add(job)
    session.add(attempt)


def interrupt_job_stage(
    session: Session,
    *,
    job_id: int,
    attempt_id: int,
    now: datetime,
) -> None:
    job = _require_job(session, job_id)
    attempt = _require_attempt(session, attempt_id)
    if job.cancel_requested_at is not None:
        _cancel_claimed_job(session, job=job, attempt=attempt, now=now)
        return

    attempt.status = AttemptStatus.INTERRUPTED
    attempt.finished_at = now
    attempt.exit_code = 130
    if job.hold_requested_at is not None:
        job.status = JobStatus.HELD
        job.held_at = now
        _add_job_event(
            session,
            job_id=job_id,
            event_type=JobEventType.HELD,
            actor=job.hold_requested_by or "scheduler",
            reason=job.hold_reason,
            now=now,
        )
    else:
        job.status = JobStatus.PENDING
    job.claimed_by = None
    job.updated_at = now
    job.finished_at = None
    session.add(job)
    session.add(attempt)


async def execute_probe_job(
    *,
    job_id: int,
    runner_id: str,
    config: AppConfig,
) -> None:
    engine = create_db_engine(config.database.url)
    now = utc_now()
    with Session(engine) as session, session.begin():
        job = _require_job(session, job_id)
        media_file = _require_media_file(session, job.media_file_id)
        attempt = claim_job_stage(session, job_id=job_id, runner_id=runner_id, now=now)
        attempt.command_json = canonical_json(build_ffprobe_command(Path(media_file.path)))
        attempt_id = _require_id(attempt)
        media_path = Path(media_file.path)
        try:
            _verify_media_snapshot(media_file, expected_fingerprint=job.source_fs_fingerprint)
        except Exception as exc:
            fail_job_stage(
                session,
                job_id=job_id,
                attempt_id=attempt_id,
                error=_scheduler_error(exc),
                exit_code=None,
                now=utc_now(),
            )
            return

    try:
        raw_probe = await asyncio.to_thread(run_ffprobe, media_path)
        normalized_probe = normalize_probe(raw_probe)
        current_snapshot = create_file_snapshot(media_path)
        if current_snapshot.fs_fingerprint != job.source_fs_fingerprint:
            raise StaleJobSourceError("Source fingerprint changed while probing.")
    except Exception as exc:
        with Session(engine) as session, session.begin():
            fail_job_stage(
                session,
                job_id=job_id,
                attempt_id=attempt_id,
                error=_scheduler_error(exc),
                exit_code=None,
                now=utc_now(),
            )
        return

    with Session(engine) as session, session.begin():
        job = _require_job(session, job_id)
        media_file = _require_media_file(session, job.media_file_id)
        probe_result = store_probe_result(
            session,
            media_file=media_file,
            raw_probe=raw_probe,
            normalized_probe=normalized_probe,
            created_at=utc_now(),
        )
        session.flush()
        _attach_probe_and_advance(
            session,
            job=job,
            media_file=media_file,
            probe_result=probe_result,
            config=config,
            attempt_id=attempt_id,
            now=utc_now(),
        )


async def execute_plan_job(
    *,
    job_id: int,
    runner_id: str,
    config: AppConfig,
) -> None:
    engine = create_db_engine(config.database.url)
    now = utc_now()
    with Session(engine) as session, session.begin():
        job = _require_job(session, job_id)
        media_file = _require_media_file(session, job.media_file_id)
        attempt = claim_job_stage(session, job_id=job_id, runner_id=runner_id, now=now)
        attempt_id = _require_id(attempt)
        input_path = Path(media_file.path)
        try:
            _verify_media_snapshot(media_file, expected_fingerprint=job.source_fs_fingerprint)
        except Exception as exc:
            fail_job_stage(
                session,
                job_id=job_id,
                attempt_id=attempt_id,
                error=_scheduler_error(exc),
                exit_code=None,
                now=utc_now(),
            )
            return

    try:
        with Session(engine) as session:
            job = _require_job(session, job_id)
            resolved_profile = _require_profile(config, job.profile_name)
            _verify_job_profile(config, job)
            context = load_planning_context(
                session,
                input_path=input_path,
                resolved_profile=resolved_profile,
            )
            if context.probe_result.probe_hash != job.probe_hash:
                raise JobPreparationError("Canonical probe no longer matches the queued job.")
            match = match_profile(context.profile, context.normalized_probe)
            if not match.matched:
                _mark_job_skipped(
                    engine,
                    job_id=job_id,
                    attempt_id=attempt_id,
                    reason=f"profile not applicable: {', '.join(match.reasons)}",
                )
                return
            resolved_template = resolve_vapoursynth_template(context.profile)
            resolved_filter = resolve_vapoursynth_filter(context.profile)
            data_dir = _config_data_dir(config)
            plan = build_plan(
                context,
                data_dir=data_dir,
                resolved_template=resolved_template,
                resolved_filter=resolved_filter,
            )
        vapoursynth_script = generate_vapoursynth_script(
            plan,
            template=resolved_template,
            user_filter=resolved_filter,
        )
        validate_script_syntax(vapoursynth_script)
        write_plan_artifacts(
            plan=plan,
            vapoursynth_script=vapoursynth_script,
            user_filter=resolved_filter,
            template=resolved_template,
        )
    except PlanArtifactConflictError as exc:
        _fail_after_external(engine, job_id=job_id, attempt_id=attempt_id, error=exc)
        return
    except (
        PlanningError,
        VapourSynthGenerationError,
        JobPreparationError,
        StaleJobProfileError,
    ) as exc:
        _fail_after_external(engine, job_id=job_id, attempt_id=attempt_id, error=exc)
        return
    except Exception as exc:
        _fail_after_external(engine, job_id=job_id, attempt_id=attempt_id, error=exc)
        return

    with Session(engine) as session, session.begin():
        job = _require_job(session, job_id)
        duplicate = session.exec(
            select(Job).where(Job.plan_hash == plan.plan_hash, Job.id != job_id)
        ).first()
        if duplicate is not None:
            _skip_claimed_job(
                session,
                job=job,
                attempt_id=attempt_id,
                reason="duplicate plan identity",
                now=utc_now(),
            )
            return
        job.plan_hash = plan.plan_hash
        job.plan_path = str(plan.artifacts.plan_json)
        job.output_path = str(plan.output_path)
        attempt = _require_attempt(session, attempt_id)
        attempt.details_json = canonical_json(
            {
                "profile_hash": plan.profile_hash,
                "probe_hash": plan.probe_hash,
                "plan_hash": plan.plan_hash,
                "artifact_path": str(plan.artifacts.artifact_dir),
            }
        )
        complete_job_stage(
            session,
            job_id=job_id,
            attempt_id=attempt_id,
            next_stage=JobStage.ENCODE,
            now=utc_now(),
        )


async def execute_encode_job(
    *,
    job_id: int,
    runner_id: str,
    config: AppConfig,
    progress_callback: ProgressCallback | None = None,
) -> None:
    del progress_callback
    engine = create_db_engine(config.database.url)
    now = utc_now()
    with Session(engine) as session, session.begin():
        job = _require_job(session, job_id)
        media_file = _require_media_file(session, job.media_file_id)
        attempt = claim_job_stage(session, job_id=job_id, runner_id=runner_id, now=now)
        attempt_id = _require_id(attempt)
        try:
            _verify_media_snapshot(media_file, expected_fingerprint=job.source_fs_fingerprint)
            plan = _load_job_plan(job)
            if plan.plan_hash != job.plan_hash:
                raise JobPreparationError("Stored plan hash does not match the queued job.")
            effective_resume = should_resume_av1an(plan.av1an)
        except Exception as exc:
            fail_job_stage(
                session,
                job_id=job_id,
                attempt_id=attempt_id,
                error=_scheduler_error(exc),
                exit_code=None,
                now=utc_now(),
            )
            return
        attempt.command_json = canonical_json(
            {
                "effective_resume": effective_resume,
                "av1an_argv": build_av1an_command(plan.av1an, resume=effective_resume),
                "mux_spec": {
                    "video_input_path": str(plan.mux.video_input_path),
                    "source_input_path": str(plan.mux.source_input_path),
                    "output_path": str(plan.mux.output_path),
                },
            }
        )
        attempt.stdout_log = str(plan.runtime.av1an_stdout_log)
        attempt.stderr_log = str(plan.runtime.av1an_stderr_log)
        attempt.temp_dir = str(plan.av1an.temp_dir)
        attempt.output_path = str(plan.output_path)

    try:
        status = await asyncio.to_thread(execute_plan, plan)
    except ExecutionInterruptedError:
        with Session(engine) as session, session.begin():
            interrupt_job_stage(session, job_id=job_id, attempt_id=attempt_id, now=utc_now())
        return
    except ExecutionError as exc:
        _fail_after_external(engine, job_id=job_id, attempt_id=attempt_id, error=exc)
        return

    with Session(engine) as session, session.begin():
        attempt = _require_attempt(session, attempt_id)
        attempt.details_json = canonical_json(
            {
                "mux_stdout_log": str(plan.runtime.mux_stdout_log),
                "mux_stderr_log": str(plan.runtime.mux_stderr_log),
                "stage_marker_path": str(plan.runtime.av1an_stage_marker),
                "encode_receipt_path": str(plan.runtime.encode_result),
                "final_execution_status": status,
            }
        )
        complete_job_stage(
            session,
            job_id=job_id,
            attempt_id=attempt_id,
            next_stage=JobStage.VALIDATE,
            now=utc_now(),
        )


async def execute_validation_job(
    *,
    job_id: int,
    runner_id: str,
    config: AppConfig,
) -> ValidationResult | None:
    engine = create_db_engine(config.database.url)
    now = utc_now()
    with Session(engine) as session, session.begin():
        job = _require_job(session, job_id)
        if (
            job.status == JobStatus.VALIDATED
            and job.stage == JobStage.PROMOTE
            and _latest_validation_passed(session, job)
        ):
            return session.get(ValidationResult, job.latest_validation_id)
        attempt = claim_job_stage(session, job_id=job_id, runner_id=runner_id, now=now)
        attempt_id = _require_id(attempt)
        try:
            plan = _load_job_plan(job)
            if plan.plan_hash != job.plan_hash:
                raise JobPreparationError("Stored plan hash does not match the queued job.")
            validation_job = _snapshot_job(job)
        except Exception as exc:
            fail_job_stage(
                session,
                job_id=job_id,
                attempt_id=attempt_id,
                error=_scheduler_error(exc),
                exit_code=None,
                now=utc_now(),
            )
            return None
        attempt.command_json = canonical_json(
            {
                "ffprobe_argv": build_ffprobe_command(plan.output_path),
                "decode_sample": plan.validation.decode_sample.model_dump(mode="json"),
            }
        )
        attempt.stdout_log = str(plan.runtime.validation_decode_stdout_log)
        attempt.stderr_log = str(plan.runtime.validation_decode_stderr_log)
        attempt.output_path = str(plan.output_path)

    try:
        report = await validate_output(
            job=validation_job,
            plan=plan,
            policy=plan.validation,
            runtime_paths=plan.runtime,
            clock=utc_now,
        )
    except OutputValidationError as exc:
        _fail_after_external(engine, job_id=job_id, attempt_id=attempt_id, error=exc)
        return None
    except Exception as exc:
        _fail_after_external(engine, job_id=job_id, attempt_id=attempt_id, error=exc)
        return None

    with Session(engine) as session, session.begin():
        job = _require_job(session, job_id)
        if job.cancel_requested_at is not None:
            attempt = _require_attempt(session, attempt_id)
            _cancel_claimed_job(session, job=job, attempt=attempt, now=utc_now())
            return None
        attempt = _require_attempt(session, attempt_id)
        result = persist_validation_result(
            session,
            job=job,
            attempt=attempt,
            report=report,
        )
        if report.passed and job.hold_requested_at is not None:
            _clear_hold_fields(job)
        attempt.details_json = canonical_json(
            {
                "result_id": result.id,
                "report_path": str(plan.runtime.validation_report),
                "plan_hash": report.plan_hash,
                "policy_hash": report.policy_hash,
                "failed_checks": failed_required_check_names(report),
            }
        )
        session.add(attempt)
        return result


def acquire_scheduler_lease(
    session: Session,
    *,
    runner_id: str,
    now: datetime,
    resume: bool = False,
) -> SchedulerState:
    state = _get_or_create_scheduler_state(session, now=now)
    if (
        state.runner_id is not None
        and state.lease_expires_at is not None
        and _datetime_after(state.lease_expires_at, now)
        and state.runner_id != runner_id
    ):
        raise SchedulerAlreadyRunningError("Another scheduler lease is still active.")

    if resume and state.mode == SchedulerMode.PAUSED:
        _request_scheduler_mode(
            state,
            mode=SchedulerMode.RUNNING,
            now=now,
            reason=None,
            increment=True,
        )
    elif state.mode == SchedulerMode.PAUSED:
        raise SchedulerControlError("Scheduler is paused. Run `avarch scheduler resume` first.")

    if state.mode in {SchedulerMode.DRAINING, SchedulerMode.STOPPING} and not _lease_active(
        state, now=now
    ):
        state.mode = SchedulerMode.RUNNING
        state.control_requested_at = None
        state.control_acknowledged_at = None
        state.control_reason = None

    state.runner_id = runner_id
    state.heartbeat_at = now
    state.lease_expires_at = now + timedelta(seconds=SCHEDULER_LEASE_SECONDS)
    state.updated_at = now
    session.add(state)
    return state


def renew_scheduler_lease(
    session: Session,
    *,
    runner_id: str,
    now: datetime,
) -> None:
    state = _get_or_create_scheduler_state(session, now=now)
    if state.runner_id != runner_id:
        raise SchedulerLeaseLostError("Scheduler lease belongs to another runner.")
    state.heartbeat_at = now
    state.lease_expires_at = now + timedelta(seconds=SCHEDULER_LEASE_SECONDS)
    state.updated_at = now
    session.add(state)


def release_scheduler_lease(
    session: Session,
    *,
    runner_id: str,
    now: datetime,
) -> None:
    state = _get_or_create_scheduler_state(session, now=now)
    if state.runner_id != runner_id:
        return
    state.runner_id = None
    state.lease_expires_at = None
    state.heartbeat_at = now
    if state.mode in {SchedulerMode.DRAINING, SchedulerMode.STOPPING}:
        state.mode = SchedulerMode.RUNNING
        state.control_requested_at = None
        state.control_acknowledged_at = None
        state.control_reason = None
    state.updated_at = now
    session.add(state)


def recover_abandoned_jobs(
    session: Session,
    *,
    new_runner_id: str,
    now: datetime,
) -> RecoverySummary:
    del new_runner_id
    recovered_jobs = 0
    interrupted_attempts = 0
    jobs = list(session.exec(select(Job).where(Job.status == JobStatus.RUNNING)).all())
    for job in jobs:
        if job.stage == JobStage.PROMOTE:
            continue
        attempt = session.exec(
            select(JobAttempt)
            .where(JobAttempt.job_id == job.id, JobAttempt.status == AttemptStatus.RUNNING)
            .order_by(col(JobAttempt.attempt_number).desc())
        ).first()
        if attempt is not None and job.cancel_requested_at is not None:
            _cancel_claimed_job(session, job=job, attempt=attempt, now=now)
            interrupted_attempts += 1
        elif attempt is not None:
            attempt.status = AttemptStatus.INTERRUPTED
            attempt.finished_at = now
            attempt.error_type = "SchedulerRecovered"
            attempt.error_message = "Running attempt was recovered after scheduler restart."
            session.add(attempt)
            interrupted_attempts += 1
            job.status = JobStatus.HELD if job.hold_requested_at is not None else JobStatus.PENDING
            if job.status == JobStatus.HELD:
                job.held_at = now
                _add_job_event(
                    session,
                    job_id=_require_id(job),
                    event_type=JobEventType.HELD,
                    actor=job.hold_requested_by or "scheduler",
                    reason=job.hold_reason,
                    now=now,
                )
            job.claimed_by = None
            job.finished_at = None
            job.updated_at = now
            session.add(job)
        else:
            job.status = JobStatus.HELD if job.hold_requested_at is not None else JobStatus.PENDING
            if job.cancel_requested_at is not None:
                job.status = JobStatus.CANCELED
                job.canceled_at = now
                job.finished_at = now
                _add_job_event(
                    session,
                    job_id=_require_id(job),
                    event_type=JobEventType.CANCELED,
                    actor=job.cancel_requested_by or "scheduler",
                    reason=job.cancel_reason,
                    now=now,
                )
            elif job.status == JobStatus.HELD:
                job.held_at = now
                _add_job_event(
                    session,
                    job_id=_require_id(job),
                    event_type=JobEventType.HELD,
                    actor=job.hold_requested_by or "scheduler",
                    reason=job.hold_reason,
                    now=now,
                )
            job.claimed_by = None
            job.updated_at = now
            session.add(job)
        recovered_jobs += 1
    return RecoverySummary(recovered_jobs=recovered_jobs, interrupted_attempts=interrupted_attempts)


async def run_scheduler(
    *,
    config: AppConfig,
    runner_id: str,
    resume: bool = False,
) -> SchedulerRunSummary:
    engine = create_db_engine(config.database.url)
    with Session(engine) as session, session.begin():
        acquire_scheduler_lease(session, runner_id=runner_id, now=utc_now(), resume=resume)
        recover_abandoned_jobs(session, new_runner_id=runner_id, now=utc_now())

    workers = {
        JobStage.PROBE: execute_probe_job,
        JobStage.PLAN: execute_plan_job,
        JobStage.ENCODE: execute_encode_job,
        JobStage.VALIDATE: execute_validation_job,
    }
    active: dict[asyncio.Task[Any], tuple[int, JobStage]] = {}
    active_job_ids: set[int] = set()
    last_heartbeat = utc_now()
    idle_since: datetime | None = None
    current_mode = SchedulerMode.RUNNING

    try:
        while True:
            now = utc_now()
            if (now - last_heartbeat).total_seconds() >= SCHEDULER_HEARTBEAT_SECONDS:
                with Session(engine) as session, session.begin():
                    renew_scheduler_lease(session, runner_id=runner_id, now=now)
                last_heartbeat = now

            done = [task for task in active if task.done()]
            for task in done:
                job_id, _stage = active.pop(task)
                active_job_ids.discard(job_id)
                try:
                    task.result()
                except asyncio.CancelledError:
                    _interrupt_running_job(engine, job_id=job_id)

            with Session(engine) as session:
                state = _get_or_create_scheduler_state(session, now=now)
                if state.runner_id != runner_id:
                    raise SchedulerLeaseLostError("Scheduler lease belongs to another runner.")
                current_mode = SchedulerMode(state.mode)
                if state.acknowledged_generation != state.control_generation:
                    with Session(engine) as ack_session, ack_session.begin():
                        ack_state = _get_or_create_scheduler_state(ack_session, now=utc_now())
                        if ack_state.runner_id == runner_id:
                            ack_state.acknowledged_generation = ack_state.control_generation
                            ack_state.control_acknowledged_at = utc_now()
                            ack_session.add(ack_state)

                canceled_active_ids: set[int] = set()
                for job_id in active_job_ids:
                    active_job = session.get(Job, job_id)
                    if active_job is not None and active_job.cancel_requested_at is not None:
                        canceled_active_ids.add(job_id)
                if canceled_active_ids:
                    for task, (job_id, _stage) in active.items():
                        if job_id in canceled_active_ids:
                            task.cancel()

                if current_mode == SchedulerMode.STOPPING:
                    for task in active:
                        task.cancel()

                if current_mode == SchedulerMode.RUNNING:
                    for job in _claimable_jobs(session, active_job_ids=active_job_ids):
                        if not _has_resource_capacity(job, active, config=config):
                            continue
                        worker = workers[job.stage]
                        job_id = _require_id(job)
                        task = asyncio.create_task(
                            worker(job_id=job_id, runner_id=runner_id, config=config)
                        )
                        active[task] = (job_id, job.stage)
                        active_job_ids.add(job_id)

            if active:
                await asyncio.sleep(SCHEDULER_CONTROL_POLL_SECONDS)
                continue

            if current_mode in {SchedulerMode.DRAINING, SchedulerMode.STOPPING}:
                break

            if current_mode == SchedulerMode.PAUSED:
                await asyncio.sleep(SCHEDULER_CONTROL_POLL_SECONDS)
                continue

            with Session(engine) as session:
                pending_count = session.exec(
                    select(Job).where(Job.status == JobStatus.PENDING)
                ).first()
            if pending_count is None:
                idle_since = idle_since or utc_now()
                if (utc_now() - idle_since).total_seconds() >= SCHEDULER_IDLE_EXIT_SECONDS:
                    break
            else:
                idle_since = None
            await asyncio.sleep(SCHEDULER_POLL_SECONDS)
    finally:
        with Session(engine) as session, session.begin():
            release_scheduler_lease(session, runner_id=runner_id, now=utc_now())

    with Session(engine) as session:
        completed = len(session.exec(select(Job).where(Job.status == JobStatus.COMPLETED)).all())
        failed = len(session.exec(select(Job).where(Job.status == JobStatus.FAILED)).all())
        skipped = len(session.exec(select(Job).where(Job.status == JobStatus.SKIPPED)).all())
    return SchedulerRunSummary(completed=completed, failed=failed, skipped=skipped, idle=True)


def pause_scheduler(session: Session, *, now: datetime, reason: str | None = None) -> None:
    state = _get_or_create_scheduler_state(session, now=now)
    if state.mode == SchedulerMode.PAUSED:
        return
    if state.mode != SchedulerMode.RUNNING:
        raise SchedulerControlError(f"Cannot pause while scheduler is {state.mode}.")
    _request_scheduler_mode(state, mode=SchedulerMode.PAUSED, now=now, reason=reason)
    session.add(state)


def resume_scheduler(session: Session, *, now: datetime) -> None:
    state = _get_or_create_scheduler_state(session, now=now)
    if state.mode == SchedulerMode.RUNNING:
        return
    if state.mode != SchedulerMode.PAUSED:
        raise SchedulerControlError(f"Cannot resume while scheduler is {state.mode}.")
    _request_scheduler_mode(state, mode=SchedulerMode.RUNNING, now=now, reason=None)
    session.add(state)


def drain_scheduler(session: Session, *, now: datetime, reason: str | None = None) -> None:
    state = _get_or_create_scheduler_state(session, now=now)
    if not _lease_active(state, now=now):
        raise SchedulerControlError("No active scheduler lease is running.")
    if state.mode != SchedulerMode.RUNNING:
        raise SchedulerControlError(f"Cannot drain while scheduler is {state.mode}.")
    _request_scheduler_mode(state, mode=SchedulerMode.DRAINING, now=now, reason=reason)
    session.add(state)


def stop_scheduler(session: Session, *, now: datetime, reason: str | None = None) -> None:
    state = _get_or_create_scheduler_state(session, now=now)
    if not _lease_active(state, now=now):
        raise SchedulerControlError("No active scheduler lease is running.")
    if state.mode not in {SchedulerMode.RUNNING, SchedulerMode.PAUSED}:
        raise SchedulerControlError(f"Cannot stop while scheduler is {state.mode}.")
    _request_scheduler_mode(state, mode=SchedulerMode.STOPPING, now=now, reason=reason)
    session.add(state)


def scheduler_status(session: Session, *, now: datetime) -> SchedulerStatus:
    state = _get_or_create_scheduler_state(session, now=now)
    counts_by_status = {
        status: len(session.exec(select(Job).where(Job.status == status)).all())
        for status in JobStatus
    }
    active_jobs = list(
        session.exec(
            select(Job)
            .where(Job.status == JobStatus.RUNNING)
            .order_by(col(Job.priority).desc(), col(Job.created_at).asc(), col(Job.id).asc())
        ).all()
    )
    cancel_pending = len(
        session.exec(
            select(Job).where(
                col(Job.cancel_requested_at).is_not(None),
                Job.status == JobStatus.RUNNING,
            )
        ).all()
    )
    hold_pending = len(
        session.exec(
            select(Job).where(
                col(Job.hold_requested_at).is_not(None),
                Job.status == JobStatus.RUNNING,
            )
        ).all()
    )
    lease_state = "inactive"
    if state.runner_id is not None:
        lease_state = "active" if _lease_active(state, now=now) else "stale"
    return SchedulerStatus(
        mode=SchedulerMode(state.mode),
        control_generation=state.control_generation,
        acknowledged_generation=state.acknowledged_generation,
        runner_id=state.runner_id,
        heartbeat_at=state.heartbeat_at,
        lease_expires_at=state.lease_expires_at,
        lease_state=lease_state,
        counts_by_status=counts_by_status,
        cancel_pending=cancel_pending,
        hold_pending=hold_pending,
        active_jobs=active_jobs,
    )


def cancel_job(
    session: Session,
    *,
    job_id: int,
    actor: str,
    now: datetime,
    reason: str | None = None,
    event_type: JobEventType = JobEventType.CANCEL_REQUESTED,
    details_json: str | None = None,
) -> None:
    job = _require_job(session, job_id)
    if job.status == JobStatus.CANCELED:
        return
    if job.status in {JobStatus.COMPLETED, JobStatus.SKIPPED}:
        raise JobControlError(f"Job {job_id} cannot be canceled from {job.status}.")
    if job.status == JobStatus.RUNNING and job.stage == JobStage.PROMOTE:
        raise JobControlError(
            "This job has an active promotion transaction. Use promotion recovery or interrupt "
            "the owning promote command."
        )

    job.cancel_requested_at = job.cancel_requested_at or now
    job.cancel_requested_by = job.cancel_requested_by or actor
    job.cancel_reason = reason
    if job.status == JobStatus.RUNNING:
        _add_job_event(
            session,
            job_id=job_id,
            event_type=event_type,
            actor=actor,
            reason=reason,
            details_json=details_json,
            now=now,
        )
    else:
        job.status = JobStatus.CANCELED
        job.canceled_at = now
        job.finished_at = now
        job.claimed_by = None
        _clear_hold_fields(job)
        _add_job_event(
            session,
            job_id=job_id,
            event_type=JobEventType.CANCELED
            if event_type == JobEventType.CANCEL_REQUESTED
            else event_type,
            actor=actor,
            reason=reason,
            details_json=details_json,
            now=now,
        )
    job.updated_at = now
    session.add(job)


def hold_job(
    session: Session,
    *,
    job_id: int,
    actor: str,
    now: datetime,
    reason: str | None = None,
) -> None:
    job = _require_job(session, job_id)
    if job.status == JobStatus.HELD:
        return
    if job.status == JobStatus.RUNNING and job.stage == JobStage.PROMOTE:
        raise JobControlError("Running promotion jobs cannot be held by the scheduler.")
    if job.status in {
        JobStatus.FAILED,
        JobStatus.CANCELED,
        JobStatus.COMPLETED,
        JobStatus.SKIPPED,
        JobStatus.VALIDATED,
    }:
        raise JobControlError(f"Job {job_id} cannot be held from {job.status}.")

    job.hold_requested_at = job.hold_requested_at or now
    job.hold_requested_by = job.hold_requested_by or actor
    job.hold_reason = reason
    if job.status == JobStatus.RUNNING:
        _add_job_event(
            session,
            job_id=job_id,
            event_type=JobEventType.HOLD_REQUESTED,
            actor=actor,
            reason=reason,
            now=now,
        )
    else:
        job.status = JobStatus.HELD
        job.held_at = now
        _add_job_event(
            session,
            job_id=job_id,
            event_type=JobEventType.HELD,
            actor=actor,
            reason=reason,
            now=now,
        )
    job.updated_at = now
    session.add(job)


def release_job(session: Session, *, job_id: int, actor: str, now: datetime) -> bool:
    job = _require_job(session, job_id)
    had_hold = job.hold_requested_at is not None or job.status == JobStatus.HELD
    if not had_hold:
        return False
    if job.status == JobStatus.HELD:
        job.status = JobStatus.PENDING
        job.finished_at = None
    _clear_hold_fields(job)
    job.updated_at = now
    _add_job_event(
        session,
        job_id=job_id,
        event_type=JobEventType.HOLD_RELEASED,
        actor=actor,
        now=now,
    )
    session.add(job)
    return True


def retry_job(
    session: Session,
    *,
    job_id: int,
    config: AppConfig,
    actor: str,
    now: datetime,
) -> JobStage:
    job = _require_job(session, job_id)
    if job.status not in {JobStatus.FAILED, JobStatus.CANCELED}:
        raise JobControlError(f"Job {job_id} cannot be retried from {job.status}.")
    next_stage = _resolve_retry_stage(session, job, config=config)
    if next_stage is None:
        raise JobControlError("Job already has a completed promotion.")
    _reset_job_for_retry(job, next_stage=next_stage, now=now)
    _add_job_event(
        session,
        job_id=job_id,
        event_type=JobEventType.RETRY_REQUESTED,
        actor=actor,
        now=now,
    )
    session.add(job)
    return next_stage


def update_job_priority(
    session: Session,
    *,
    job_id: int,
    priority: int,
    actor: str,
    now: datetime,
) -> None:
    job = _require_job(session, job_id)
    if job.status not in {JobStatus.PENDING, JobStatus.HELD}:
        raise JobControlError(f"Job {job_id} priority cannot change from {job.status}.")
    old_priority = job.priority
    job.priority = priority
    job.updated_at = now
    _add_job_event(
        session,
        job_id=job_id,
        event_type=JobEventType.PRIORITY_CHANGED,
        actor=actor,
        details_json=canonical_json({"old_priority": old_priority, "new_priority": priority}),
        now=now,
    )
    session.add(job)


def retry_failed_jobs(
    session: Session,
    *,
    config: AppConfig,
    now: datetime,
) -> RetrySummary:
    jobs = list(session.exec(select(Job).where(Job.status == JobStatus.FAILED)).all())
    eligible = 0
    reset_to_probe = 0
    reset_to_plan = 0
    reset_to_encode = 0
    reset_to_validate = 0
    requires_requeue = 0
    for job in jobs:
        try:
            next_stage = _resolve_retry_stage(session, job, config=config)
        except JobControlError:
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
        _reset_job_for_retry(job, next_stage=next_stage, now=now)
        session.add(job)
    return RetrySummary(
        eligible=eligible,
        reset_to_probe=reset_to_probe,
        reset_to_plan=reset_to_plan,
        reset_to_encode=reset_to_encode,
        reset_to_validate=reset_to_validate,
        requires_requeue=requires_requeue,
    )


def clear_queue(
    session: Session,
    *,
    actor: str,
    now: datetime,
    job_ids: set[int] | None = None,
    statuses: set[JobStatus] | None = None,
    stages: set[JobStage] | None = None,
    profile: str | None = None,
    all_jobs: bool = False,
    cancel_running: bool = False,
    confirm: bool = False,
) -> QueueClearSummary:
    jobs = _select_jobs(
        session,
        job_ids=job_ids,
        statuses=statuses,
        stages=stages,
        profile=profile,
        all_jobs=all_jobs,
    )
    operation_id = uuid.uuid4().hex
    immediate = 0
    running = 0
    promotion_excluded = 0
    completed_excluded = 0
    changed = 0
    for job in jobs:
        if job.status in {JobStatus.COMPLETED, JobStatus.SKIPPED, JobStatus.CANCELED}:
            completed_excluded += 1
            continue
        if job.status == JobStatus.RUNNING and job.stage == JobStage.PROMOTE:
            promotion_excluded += 1
            continue
        if job.status == JobStatus.RUNNING and not cancel_running:
            continue
        if job.status == JobStatus.RUNNING:
            running += 1
        else:
            immediate += 1
        if confirm:
            cancel_job(
                session,
                job_id=_require_id(job),
                actor=actor,
                now=now,
                reason="queue clear",
                event_type=JobEventType.QUEUE_CLEARED,
                details_json=canonical_json({"operation_id": operation_id}),
            )
            changed += 1
    return QueueClearSummary(
        matched=len(jobs),
        immediate_cancel=immediate,
        running_requests=running,
        promotion_excluded=promotion_excluded,
        completed_excluded=completed_excluded,
        changed=changed,
        operation_id=operation_id,
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
    jobs = _select_jobs(
        session,
        job_ids=job_ids,
        statuses=statuses,
        stages=stages,
        profile=profile,
        all_jobs=all_jobs,
    )
    retryable = 0
    requires_requeue = 0
    reset_to_probe = 0
    reset_to_plan = 0
    reset_to_encode = 0
    reset_to_validate = 0
    return_to_promote = 0
    for job in jobs:
        if job.status not in {JobStatus.FAILED, JobStatus.CANCELED}:
            continue
        try:
            next_stage = _resolve_retry_stage(session, job, config=config)
        except JobControlError:
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
            _reset_job_for_retry(job, next_stage=next_stage, now=now)
            _add_job_event(
                session,
                job_id=_require_id(job),
                event_type=JobEventType.RETRY_REQUESTED,
                actor=actor,
                now=now,
            )
            session.add(job)
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


def new_runner_id() -> str:
    return uuid.uuid4().hex


def resource_for_stage(stage: JobStage) -> ResourceClass:
    if stage in {JobStage.PROBE, JobStage.PLAN, JobStage.VALIDATE}:
        return ResourceClass.CHEAP
    if stage == JobStage.ENCODE:
        return ResourceClass.HEAVY_AV1AN
    if stage == JobStage.PROMOTE:
        return ResourceClass.FILE_OP
    raise ValueError(f"Unsupported job stage: {stage}")


@dataclass(frozen=True, slots=True)
class _PlanningIdentity:
    profile_hash: str
    vapoursynth_identity_hash: str
    execution_identity_hash: str


def _planning_identity(resolved_profile: ResolvedProfile) -> _PlanningIdentity:
    profile = resolved_profile.profile
    resolved_template = resolve_vapoursynth_template(profile)
    resolved_filter = resolve_vapoursynth_filter(profile)
    mode = (
        "custom_filter"
        if resolved_filter is not None
        else "custom_template"
        if resolved_template is not None
        else "generated"
    )
    template_hash = resolved_template.template_hash if resolved_template is not None else None
    script_hash = resolved_filter.script_hash if resolved_filter is not None else None
    vapoursynth_identity_hash = build_vapoursynth_identity_hash(
        generator_version=GENERATOR_VERSION,
        mode=mode,
        output_format="YUV420P10",
        resize_filter="spline36",
        template_hash=template_hash,
        script_hash=script_hash,
        filter_entrypoint=resolved_filter.entrypoint if resolved_filter is not None else None,
        filter_api_version=resolved_filter.api_version if resolved_filter is not None else None,
    )
    execution_identity = build_execution_identity()
    return _PlanningIdentity(
        profile_hash=build_profile_hash(
            profile,
            template_hash=template_hash,
            script_hash=script_hash,
        ),
        vapoursynth_identity_hash=vapoursynth_identity_hash,
        execution_identity_hash=execution_identity.identity_hash,
    )


def _attach_probe_and_advance(
    session: Session,
    *,
    job: Job,
    media_file: MediaFile,
    probe_result: ProbeResult,
    config: AppConfig,
    attempt_id: int,
    now: datetime,
) -> None:
    identity = _planning_identity(_require_profile(config, job.profile_name))
    queue_key = build_queue_key(
        media_path=Path(media_file.path),
        source_fs_fingerprint=media_file.fs_fingerprint,
        profile_name=job.profile_name,
        profile_hash=identity.profile_hash,
        probe_hash=probe_result.probe_hash,
        vapoursynth_identity_hash=identity.vapoursynth_identity_hash,
        execution_identity_hash=identity.execution_identity_hash,
    )
    duplicate = session.exec(
        select(Job).where(Job.queue_key == queue_key, Job.id != job.id)
    ).first()
    if duplicate is not None:
        _skip_claimed_job(
            session,
            job=job,
            attempt_id=attempt_id,
            reason="duplicate queue identity",
            now=now,
        )
        return
    job.queue_key = queue_key
    job.probe_result_id = probe_result.id
    job.probe_hash = probe_result.probe_hash
    complete_job_stage(
        session,
        job_id=_require_id(job),
        attempt_id=attempt_id,
        next_stage=JobStage.PLAN,
        now=now,
    )


def _mark_job_skipped(
    engine: Any,
    *,
    job_id: int,
    attempt_id: int,
    reason: str,
) -> None:
    with Session(engine) as session, session.begin():
        job = _require_job(session, job_id)
        _skip_claimed_job(session, job=job, attempt_id=attempt_id, reason=reason, now=utc_now())


def _skip_claimed_job(
    session: Session,
    *,
    job: Job,
    attempt_id: int,
    reason: str,
    now: datetime,
) -> None:
    attempt = _require_attempt(session, attempt_id)
    attempt.status = AttemptStatus.COMPLETED
    attempt.finished_at = now
    attempt.details_json = canonical_json({"skip_reason": reason})
    job.status = JobStatus.SKIPPED
    job.claimed_by = None
    job.skip_reason = reason
    job.updated_at = now
    job.finished_at = now
    session.add(job)
    session.add(attempt)


def _fail_after_external(
    engine: Any,
    *,
    job_id: int,
    attempt_id: int,
    error: Exception,
) -> None:
    with Session(engine) as session, session.begin():
        fail_job_stage(
            session,
            job_id=job_id,
            attempt_id=attempt_id,
            error=error,
            exit_code=None,
            now=utc_now(),
        )


def _load_job_plan(job: Job) -> TranscodePlan:
    if job.plan_path is None or job.plan_hash is None:
        raise JobPreparationError("Queued encode job has no persisted plan.")
    path = Path(job.plan_path)
    try:
        plan = TranscodePlan.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError, ValueError) as exc:
        raise JobPreparationError(f"Unable to load queued plan: {path}") from exc
    if plan.plan_hash != job.plan_hash:
        raise JobPreparationError("Queued plan artifact does not match stored plan hash.")
    return plan


def _plan_artifact_valid(job: Job) -> bool:
    try:
        _load_job_plan(job)
    except JobPreparationError:
        return False
    return True


def _verify_media_snapshot(media_file: MediaFile, *, expected_fingerprint: str) -> None:
    if _status_value(media_file.status) == MediaFileStatus.MISSING.value:
        raise StaleJobSourceError("Source file is marked missing.")
    try:
        snapshot = create_file_snapshot(Path(media_file.path))
    except OSError as exc:
        raise StaleJobSourceError(f"Unable to inspect source file: {media_file.path}") from exc
    if snapshot.fs_fingerprint != expected_fingerprint:
        raise StaleJobSourceError("Source fingerprint changed after enqueue.")


def _verify_job_profile(config: AppConfig, job: Job) -> None:
    identity = _planning_identity(_require_profile(config, job.profile_name))
    if identity.profile_hash != job.profile_hash:
        raise StaleJobProfileError("Profile changed after enqueue; re-enqueue this work.")


def _claimable_jobs(session: Session, *, active_job_ids: set[int]) -> list[Job]:
    jobs = list(
        session.exec(
            select(Job)
            .where(Job.status == JobStatus.PENDING)
            .order_by(col(Job.priority).desc(), col(Job.created_at).asc(), col(Job.id).asc())
        ).all()
    )
    return [job for job in jobs if job.id not in active_job_ids]


def _has_resource_capacity(
    job: Job,
    active: dict[asyncio.Task[Any], tuple[int, JobStage]],
    *,
    config: AppConfig,
) -> bool:
    cheap = sum(
        1
        for _job_id, stage in active.values()
        if stage in {JobStage.PROBE, JobStage.PLAN, JobStage.VALIDATE}
    )
    av1an = sum(1 for _job_id, stage in active.values() if stage == JobStage.ENCODE)
    if job.stage in {JobStage.PROBE, JobStage.PLAN, JobStage.VALIDATE}:
        return cheap < config.resources.cheap_workers
    if job.stage == JobStage.ENCODE:
        return av1an < config.resources.av1an_jobs
    return False


def _get_or_create_scheduler_state(session: Session, *, now: datetime) -> SchedulerState:
    state = session.get(SchedulerState, 1)
    if state is None:
        state = SchedulerState(id=1, mode=SchedulerMode.RUNNING, updated_at=now)
        session.add(state)
        session.flush()
    return state


def _request_scheduler_mode(
    state: SchedulerState,
    *,
    mode: SchedulerMode,
    now: datetime,
    reason: str | None,
    increment: bool = True,
) -> None:
    state.mode = mode
    if increment:
        state.control_generation += 1
    state.control_requested_at = now
    state.control_acknowledged_at = None
    state.control_reason = reason
    state.updated_at = now


def _lease_active(state: SchedulerState, *, now: datetime) -> bool:
    return (
        state.runner_id is not None
        and state.lease_expires_at is not None
        and _datetime_after(state.lease_expires_at, now)
    )


def _datetime_after(left: datetime, right: datetime) -> bool:
    return left.replace(tzinfo=None) > right.replace(tzinfo=None)


def _add_job_event(
    session: Session,
    *,
    job_id: int,
    event_type: JobEventType,
    actor: str,
    now: datetime,
    reason: str | None = None,
    details_json: str | None = None,
) -> None:
    session.add(
        JobEvent(
            job_id=job_id,
            event_type=event_type,
            actor=actor,
            reason=reason,
            details_json=details_json,
            created_at=now,
        )
    )


def _cancel_claimed_job(
    session: Session,
    *,
    job: Job,
    attempt: JobAttempt,
    now: datetime,
) -> None:
    job_id = _require_id(job)
    attempt.status = AttemptStatus.CANCELED
    attempt.finished_at = now
    attempt.exit_code = 130
    job.status = JobStatus.CANCELED
    job.claimed_by = None
    job.canceled_at = now
    job.finished_at = now
    job.updated_at = now
    _clear_hold_fields(job)
    _add_job_event(
        session,
        job_id=job_id,
        event_type=JobEventType.CANCELED,
        actor=job.cancel_requested_by or "scheduler",
        reason=job.cancel_reason,
        now=now,
    )
    session.add(job)
    session.add(attempt)


def _clear_hold_fields(job: Job) -> None:
    job.hold_requested_at = None
    job.hold_requested_by = None
    job.hold_reason = None
    job.held_at = None


def _clear_cancel_fields(job: Job) -> None:
    job.cancel_requested_at = None
    job.cancel_requested_by = None
    job.cancel_reason = None
    job.canceled_at = None


def _reset_job_for_retry(job: Job, *, next_stage: JobStage, now: datetime) -> None:
    _clear_cancel_fields(job)
    _clear_hold_fields(job)
    job.status = JobStatus.VALIDATED if next_stage == JobStage.PROMOTE else JobStatus.PENDING
    job.stage = next_stage
    job.last_error_type = None
    job.last_error_message = None
    job.claimed_by = None
    job.finished_at = None
    job.updated_at = now


def _resolve_retry_stage(
    session: Session,
    job: Job,
    *,
    config: AppConfig,
) -> JobStage | None:
    media_file = session.get(MediaFile, job.media_file_id)
    if media_file is None or _status_value(media_file.status) == MediaFileStatus.MISSING.value:
        raise JobControlError("Source file is missing; scan and enqueue new work.")
    if media_file.fs_fingerprint != job.source_fs_fingerprint:
        raise JobControlError("Source identity changed; scan and enqueue new work.")
    try:
        _verify_job_profile(config, job)
    except StaleJobProfileError as exc:
        raise JobControlError(str(exc)) from exc

    canonical_probe = get_canonical_probe_result(session, media_file)
    if canonical_probe is None or canonical_probe.probe_hash != job.probe_hash:
        return JobStage.PROBE
    if job.plan_path is None or job.plan_hash is None or not _plan_artifact_valid(job):
        return JobStage.PLAN
    validation = _latest_validation(session, job)
    if _output_exists(job):
        if validation is not None and validation.passed:
            if _has_completed_promotion(session, job):
                return None
            return JobStage.PROMOTE
        return JobStage.VALIDATE
    return JobStage.ENCODE


def _latest_validation(session: Session, job: Job) -> ValidationResult | None:
    if job.latest_validation_id is None:
        return None
    result = session.get(ValidationResult, job.latest_validation_id)
    if result is None or result.job_id != job.id:
        return None
    if result.plan_hash != job.plan_hash or result.output_path != job.output_path:
        return None
    return result


def _output_exists(job: Job) -> bool:
    return job.output_path is not None and Path(job.output_path).is_file()


def _has_completed_promotion(session: Session, job: Job) -> bool:
    from avarch.models.db import PromotionRecord
    from avarch.models.promotion import PromotionStatus

    if job.id is None:
        return False
    return (
        session.exec(
            select(PromotionRecord).where(
                PromotionRecord.job_id == job.id,
                PromotionRecord.status == PromotionStatus.COMPLETED,
            )
        ).first()
        is not None
    )


def _select_jobs(
    session: Session,
    *,
    job_ids: set[int] | None,
    statuses: set[JobStatus] | None,
    stages: set[JobStage] | None,
    profile: str | None,
    all_jobs: bool,
) -> list[Job]:
    if not all_jobs and not job_ids and not statuses and not stages and profile is None:
        raise JobControlError("At least one selector is required.")
    statement = select(Job).order_by(
        col(Job.priority).desc(),
        col(Job.created_at).asc(),
        col(Job.id).asc(),
    )
    if job_ids:
        statement = statement.where(col(Job.id).in_(job_ids))
    if statuses:
        statement = statement.where(col(Job.status).in_(statuses))
    if stages:
        statement = statement.where(col(Job.stage).in_(stages))
    if profile is not None:
        statement = statement.where(Job.profile_name == profile)
    if all_jobs:
        statement = statement.where(
            col(Job.status).not_in(
                [JobStatus.COMPLETED, JobStatus.SKIPPED, JobStatus.CANCELED]
            )
        )
    return list(session.exec(statement).all())


def _interrupt_running_job(engine: Any, *, job_id: int) -> None:
    with Session(engine) as session, session.begin():
        job = session.get(Job, job_id)
        if job is None or job.status != JobStatus.RUNNING:
            return
        attempt = session.exec(
            select(JobAttempt)
            .where(JobAttempt.job_id == job_id, JobAttempt.status == AttemptStatus.RUNNING)
            .order_by(col(JobAttempt.attempt_number).desc())
        ).first()
        if attempt is None:
            job.status = JobStatus.PENDING
            job.claimed_by = None
            job.updated_at = utc_now()
            session.add(job)
            return
        interrupt_job_stage(session, job_id=job_id, attempt_id=_require_id(attempt), now=utc_now())


def _scheduler_error(error: Exception) -> Exception:
    if isinstance(error, SchedulerError | ProbeError):
        return error
    return JobPreparationError(str(error))


def _latest_validation_passed(session: Session, job: Job) -> bool:
    if job.latest_validation_id is None:
        return False
    result = session.get(ValidationResult, job.latest_validation_id)
    return (
        result is not None
        and result.job_id == job.id
        and result.plan_hash == job.plan_hash
        and result.output_path == job.output_path
        and result.passed
    )


def _snapshot_job(job: Job) -> Job:
    return Job(
        id=job.id,
        media_file_id=job.media_file_id,
        profile_name=job.profile_name,
        profile_hash=job.profile_hash,
        source_fs_fingerprint=job.source_fs_fingerprint,
        queue_key=job.queue_key,
        probe_result_id=job.probe_result_id,
        probe_hash=job.probe_hash,
        plan_hash=job.plan_hash,
        plan_path=job.plan_path,
        output_path=job.output_path,
        latest_validation_id=job.latest_validation_id,
        latest_promotion_id=job.latest_promotion_id,
        status=JobStatus(job.status),
        stage=JobStage(job.stage),
        priority=job.priority,
        attempts=job.attempts,
        claimed_by=job.claimed_by,
        last_error_type=job.last_error_type,
        last_error_message=job.last_error_message,
        skip_reason=job.skip_reason,
        cancel_requested_at=job.cancel_requested_at,
        cancel_requested_by=job.cancel_requested_by,
        cancel_reason=job.cancel_reason,
        canceled_at=job.canceled_at,
        hold_requested_at=job.hold_requested_at,
        hold_requested_by=job.hold_requested_by,
        hold_reason=job.hold_reason,
        held_at=job.held_at,
        created_at=job.created_at,
        updated_at=job.updated_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


def _require_profile(config: AppConfig, profile_name: str) -> ResolvedProfile:
    try:
        return ProfileRegistry.from_config(config).get(profile_name)
    except UnknownProfileError as exc:
        raise JobPreparationError(f"Unknown profile: {profile_name}") from exc
    except Exception as exc:
        raise JobPreparationError(str(exc)) from exc


def _require_job(session: Session, job_id: int) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise JobClaimError(f"Job not found: {job_id}")
    return job


def _require_attempt(session: Session, attempt_id: int) -> JobAttempt:
    attempt = session.get(JobAttempt, attempt_id)
    if attempt is None:
        raise JobClaimError(f"Job attempt not found: {attempt_id}")
    return attempt


def _require_media_file(session: Session, media_file_id: int) -> MediaFile:
    media_file = session.get(MediaFile, media_file_id)
    if media_file is None:
        raise JobPreparationError(f"Media file not found: {media_file_id}")
    return media_file


def _require_id(value: Any) -> int:
    identifier = getattr(value, "id", None)
    if not isinstance(identifier, int):
        raise RuntimeError("Expected a persisted row id.")
    return identifier


def _status_value(status: MediaFileStatus | str) -> str:
    if isinstance(status, MediaFileStatus):
        return status.value
    return status


def _config_data_dir(config: AppConfig) -> Path:
    return config.app.data_dir
