from __future__ import annotations

import asyncio
import hashlib
import os
import socket
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session

from avarch.adapters.filesystem.plans import (
    PlanArtifactConflictError,
    PlanArtifactLoadError,
    load_plan_artifact,
    write_plan_artifacts,
)
from avarch.adapters.sqlite import job_control
from avarch.adapters.sqlite import job_transitions as job_transition_adapter
from avarch.adapters.sqlite import scheduler_state as scheduler_state_adapter
from avarch.adapters.sqlite.db import create_db_engine
from avarch.adapters.sqlite.inventory import (
    MediaFileNotFoundError,
)
from avarch.adapters.sqlite.inventory import (
    require_media_file as require_inventory_media_file,
)
from avarch.adapters.sqlite.job_transitions import (
    cancel_claimed_job,
    claim_job_stage,
    clear_hold_fields,
    complete_job_stage,
    fail_job_stage,
    interrupt_job_stage,
    interrupt_running_job,
    require_attempt,
    require_job,
)
from avarch.adapters.sqlite.models import (
    Job,
    MediaFile,
    MediaFileStatus,
    ProbeResult,
    ValidationResult,
)
from avarch.adapters.sqlite.planning import load_planning_context
from avarch.adapters.sqlite.probes import get_canonical_probe_result, store_probe_result
from avarch.adapters.sqlite.promotions import has_completed_promotion
from avarch.adapters.sqlite.queue import (
    QueueSelectionError,
    claimable_jobs,
    create_queue_job,
    enqueue_candidate_media_files,
    failed_jobs_for_retry,
    find_existing_queue_job,
    plan_hash_conflicts_with_other_job,
    queue_key_conflicts_with_other_job,
    select_queue_jobs,
)
from avarch.adapters.sqlite.rejection_cleanup import (
    RejectedOutputCleanupError,
    cleanup_rejected_output,
)
from avarch.adapters.sqlite.scheduler_state import (
    active_jobs_with_cancel_requested,
    active_scheduler_jobs,
    get_or_create_scheduler_state,
    has_pending_jobs,
    job_counts_by_status,
    pending_cancel_count,
    pending_hold_count,
    terminal_job_counts,
)
from avarch.adapters.sqlite.validations import (
    latest_validation,
    persist_validation_result,
)
from avarch.config import AppConfig
from avarch.contracts import QUEUE_CONTRACT
from avarch.domain.jobs import (
    JobEventType,
    JobStage,
    JobStatus,
    job_can_retry,
    job_has_passed_validation,
    job_is_running,
    job_is_running_promotion,
    job_is_terminal_history,
)
from avarch.domain.scheduler import (
    ResourceCapacity,
    SchedulerMode,
    has_resource_capacity,
)
from avarch.domain.size import SizeDecision, SizePolicy, evaluate_size_policy
from avarch.execution import build_av1an_command, execute_plan, should_resume_av1an
from avarch.models.execution import ExecutionError, ExecutionInterruptedError
from avarch.models.plan import TranscodePlan
from avarch.planner import (
    PlanningError,
    build_execution_identity,
    build_plan,
    build_profile_hash,
    match_profile,
)
from avarch.probe import (
    ProbeError,
    build_ffprobe_command,
    normalize_probe,
    run_ffprobe,
)
from avarch.profiles.registry import ProfileRegistry, ResolvedProfile, UnknownProfileError
from avarch.promoter import promote_job, recover_promotion
from avarch.scanner import create_file_snapshot
from avarch.serialization import canonical_json
from avarch.validation import (
    ValidationError as OutputValidationError,
)
from avarch.validation import (
    failed_check_summary,
    failed_required_check_names,
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

SCHEDULER_HEARTBEAT_SECONDS = 5.0
SCHEDULER_POLL_SECONDS = 1.0
SCHEDULER_IDLE_EXIT_SECONDS = 2.0
SCHEDULER_CONTROL_POLL_SECONDS = 0.5
MAX_CLI_LOG_TAIL_BYTES = 64 * 1024

ProgressCallback = Callable[[int, float | None], None]
JobWorker = Callable[..., Awaitable[Any]]


class SchedulerError(RuntimeError):
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
    media_files = enqueue_candidate_media_files(session, media_file_ids=media_file_ids)
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


async def execute_probe_job(
    *,
    job_id: int,
    runner_id: str,
    config: AppConfig,
) -> None:
    engine = create_db_engine(config.database.url)
    now = utc_now()
    with Session(engine) as session, session.begin():
        job = require_job(session, job_id)
        media_file = _source_media_file(session, job.media_file_id)
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
        job = require_job(session, job_id)
        media_file = _source_media_file(session, job.media_file_id)
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
        job = require_job(session, job_id)
        media_file = _source_media_file(session, job.media_file_id)
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
            job = require_job(session, job_id)
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
                job_transition_adapter.mark_job_skipped(
                    engine,
                    job_id=job_id,
                    attempt_id=attempt_id,
                    reason=f"profile not applicable: {', '.join(match.reasons)}",
                    now=utc_now(),
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
        job_transition_adapter.fail_job_after_external(
            engine,
            job_id=job_id,
            attempt_id=attempt_id,
            error=exc,
            now=utc_now(),
        )
        return
    except (
        PlanningError,
        VapourSynthGenerationError,
        JobPreparationError,
        StaleJobProfileError,
    ) as exc:
        job_transition_adapter.fail_job_after_external(
            engine,
            job_id=job_id,
            attempt_id=attempt_id,
            error=exc,
            now=utc_now(),
        )
        return
    except Exception as exc:
        job_transition_adapter.fail_job_after_external(
            engine,
            job_id=job_id,
            attempt_id=attempt_id,
            error=exc,
            now=utc_now(),
        )
        return

    with Session(engine) as session, session.begin():
        job = require_job(session, job_id)
        if plan_hash_conflicts_with_other_job(session, plan_hash=plan.plan_hash, job_id=job_id):
            job_transition_adapter.skip_claimed_job(
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
        attempt = require_attempt(session, attempt_id)
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
        job = require_job(session, job_id)
        media_file = _source_media_file(session, job.media_file_id)
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
        job_transition_adapter.fail_job_after_external(
            engine,
            job_id=job_id,
            attempt_id=attempt_id,
            error=exc,
            now=utc_now(),
        )
        return

    with Session(engine) as session, session.begin():
        attempt = require_attempt(session, attempt_id)
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
        job = require_job(session, job_id)
        existing_validation = latest_validation(session, job)
        if (
            job_has_passed_validation(job.status, job.stage)
            and existing_validation is not None
            and existing_validation.passed
        ):
            return existing_validation
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
        job_transition_adapter.fail_job_after_external(
            engine,
            job_id=job_id,
            attempt_id=attempt_id,
            error=exc,
            now=utc_now(),
        )
        return None
    except Exception as exc:
        job_transition_adapter.fail_job_after_external(
            engine,
            job_id=job_id,
            attempt_id=attempt_id,
            error=exc,
            now=utc_now(),
        )
        return None

    with Session(engine) as session, session.begin():
        job = require_job(session, job_id)
        if job.cancel_requested_at is not None:
            attempt = require_attempt(session, attempt_id)
            cancel_claimed_job(session, job=job, attempt=attempt, now=utc_now())
            return None
        attempt = require_attempt(session, attempt_id)
        failed_checks = failed_required_check_names(report)
        result = persist_validation_result(
            session,
            job=job,
            attempt=attempt,
            report=report,
            report_path=plan.runtime.validation_report,
            failed_checks=failed_checks,
            failed_summary=failed_check_summary(report),
        )
        if report.passed:
            _apply_size_policy_after_validation(
                job=job,
                plan=plan,
                report=report,
                config=config,
                now=utc_now(),
            )
        if report.passed and job.hold_requested_at is not None:
            clear_hold_fields(job)
        return result


async def execute_promotion_job(
    *,
    job_id: int,
    runner_id: str,
    config: AppConfig,
) -> None:
    engine = create_db_engine(config.database.url)
    with Session(engine) as session:
        job = require_job(session, job_id)
        recovering = job.status == JobStatus.PROMOTING
    if recovering:
        await recover_promotion(job_id=job_id, config=config, owner_token=runner_id)
        return
    await promote_job(job_id=job_id, config=config, owner_token=runner_id)


async def run_scheduler(
    *,
    config: AppConfig,
    runner_id: str,
    resume: bool = False,
) -> SchedulerRunSummary:
    engine = create_db_engine(config.database.url)
    with Session(engine) as session, session.begin():
        scheduler_state_adapter.acquire_scheduler_lease(
            session,
            runner_id=runner_id,
            now=utc_now(),
            resume=resume,
        )
        job_transition_adapter.recover_abandoned_jobs(
            session,
            now=utc_now(),
            encoded_output_exists=_encoded_output_exists,
        )

    workers = {
        JobStage.PROBE: execute_probe_job,
        JobStage.PLAN: execute_plan_job,
        JobStage.ENCODE: execute_encode_job,
        JobStage.VALIDATE: execute_validation_job,
        JobStage.PROMOTE: execute_promotion_job,
    }
    active: dict[asyncio.Task[Any], tuple[int, JobStage]] = {}
    active_job_ids: set[int] = set()
    last_heartbeat = utc_now()
    idle_since: datetime | None = None
    current_mode = SchedulerMode.RUNNING
    resource_capacity = ResourceCapacity(
        cheap_workers=config.resources.cheap_workers,
        av1an_jobs=config.resources.av1an_jobs,
        file_ops=config.resources.file_ops,
    )

    try:
        while True:
            now = utc_now()
            if (now - last_heartbeat).total_seconds() >= SCHEDULER_HEARTBEAT_SECONDS:
                with Session(engine) as session, session.begin():
                    scheduler_state_adapter.renew_scheduler_lease(
                        session,
                        runner_id=runner_id,
                        now=now,
                    )
                last_heartbeat = now

            done = [task for task in active if task.done()]
            for task in done:
                job_id, _stage = active.pop(task)
                active_job_ids.discard(job_id)
                try:
                    task.result()
                except asyncio.CancelledError:
                    interrupt_running_job(engine, job_id=job_id, now=utc_now())

            with Session(engine) as session:
                state = get_or_create_scheduler_state(session, now=now)
                if state.runner_id != runner_id:
                    raise scheduler_state_adapter.SchedulerLeaseLostError(
                        "Scheduler lease belongs to another runner."
                    )
                current_mode = SchedulerMode(state.mode)
                if state.acknowledged_generation != state.control_generation:
                    with Session(engine) as ack_session, ack_session.begin():
                        scheduler_state_adapter.acknowledge_scheduler_control(
                            ack_session,
                            runner_id=runner_id,
                            now=utc_now(),
                        )

                canceled_active_ids = active_jobs_with_cancel_requested(
                    session,
                    job_ids=active_job_ids,
                )
                if canceled_active_ids:
                    for task, (job_id, _stage) in active.items():
                        if job_id in canceled_active_ids:
                            task.cancel()

                if current_mode == SchedulerMode.STOPPING:
                    for task in active:
                        task.cancel()

                if current_mode == SchedulerMode.RUNNING:
                    for job in claimable_jobs(session, active_job_ids=active_job_ids):
                        if not has_resource_capacity(
                            job.stage,
                            (stage for _job_id, stage in active.values()),
                            capacity=resource_capacity,
                        ):
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
                pending = has_pending_jobs(session)
            if not pending:
                idle_since = idle_since or utc_now()
                if (utc_now() - idle_since).total_seconds() >= SCHEDULER_IDLE_EXIT_SECONDS:
                    break
            else:
                idle_since = None
            await asyncio.sleep(SCHEDULER_POLL_SECONDS)
    finally:
        with Session(engine) as session, session.begin():
            scheduler_state_adapter.release_scheduler_lease(
                session,
                runner_id=runner_id,
                now=utc_now(),
            )

    with Session(engine) as session:
        terminal_counts = terminal_job_counts(session)
    return SchedulerRunSummary(
        completed=terminal_counts.completed,
        failed=terminal_counts.failed,
        skipped=terminal_counts.skipped,
        idle=True,
    )


def scheduler_status(session: Session, *, now: datetime) -> SchedulerStatus:
    state = get_or_create_scheduler_state(session, now=now)
    counts_by_status = job_counts_by_status(session)
    active_jobs = active_scheduler_jobs(session)
    cancel_pending = pending_cancel_count(session)
    hold_pending = pending_hold_count(session)
    lease_state = "inactive"
    if state.runner_id is not None:
        lease_state = "active" if scheduler_state_adapter.lease_active(state, now=now) else "stale"
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
    operation_id = uuid.uuid4().hex
    immediate = 0
    running = 0
    promotion_excluded = 0
    completed_excluded = 0
    changed = 0
    for job in jobs:
        if job_is_terminal_history(job.status):
            completed_excluded += 1
            continue
        if job_is_running_promotion(job.status, job.stage):
            promotion_excluded += 1
            continue
        if job_is_running(job.status) and not cancel_running:
            continue
        if job_is_running(job.status):
            running += 1
        else:
            immediate += 1
        if confirm:
            job_control.cancel_job(
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


def new_runner_id() -> str:
    return uuid.uuid4().hex


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
    if queue_key_conflicts_with_other_job(session, queue_key=queue_key, job_id=job.id):
        job_transition_adapter.skip_claimed_job(
            session,
            job=job,
            attempt_id=attempt_id,
            reason="duplicate queue identity",
            now=now,
        )
        return
    job_transition_adapter.attach_probe_and_advance(
        session,
        job=job,
        probe_result_id=probe_result.id,
        probe_hash=probe_result.probe_hash,
        queue_key=queue_key,
        attempt_id=attempt_id,
        now=now,
    )


def _load_job_plan(job: Job) -> TranscodePlan:
    if job.plan_path is None or job.plan_hash is None:
        raise JobPreparationError("Queued encode job has no persisted plan.")
    path = Path(job.plan_path)
    try:
        plan = load_plan_artifact(path)
    except PlanArtifactLoadError as exc:
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


def _apply_size_policy_after_validation(
    *,
    job: Job,
    plan: TranscodePlan,
    report: Any,
    config: AppConfig,
    now: datetime,
) -> None:
    profile = _require_profile(config, job.profile_name).profile
    encoded_size = (
        report.observed.output_size_bytes
        if report.observed is not None and report.observed.output_size_bytes is not None
        else Path(report.output_path).stat().st_size
    )
    decision = evaluate_size_policy(
        plan.validation.source_size_bytes,
        encoded_size,
        SizePolicy(
            require_smaller=profile.promotion.require_smaller,
            minimum_savings_percent=profile.promotion.minimum_savings_percent,
        ),
    )
    if decision == SizeDecision.ACCEPT:
        return
    try:
        cleanup_rejected_output(
            job,
            encoded_path=Path(report.output_path),
            decision=decision,
            profile=profile,
            now=now,
        )
    except RejectedOutputCleanupError:
        return


def _resolve_retry_stage(
    session: Session,
    job: Job,
    *,
    config: AppConfig,
) -> JobStage | None:
    try:
        media_file = require_inventory_media_file(session, media_file_id=job.media_file_id)
    except MediaFileNotFoundError as exc:
        raise job_control.JobControlError(
            "Source file is missing; scan and enqueue new work."
        ) from exc
    if _status_value(media_file.status) == MediaFileStatus.MISSING.value:
        raise job_control.JobControlError("Source file is missing; scan and enqueue new work.")
    if media_file.fs_fingerprint != job.source_fs_fingerprint:
        raise job_control.JobControlError("Source identity changed; scan and enqueue new work.")
    try:
        _verify_job_profile(config, job)
    except StaleJobProfileError as exc:
        raise job_control.JobControlError(str(exc)) from exc
    identity = _planning_identity(_require_profile(config, job.profile_name))

    canonical_probe = get_canonical_probe_result(session, media_file)
    if canonical_probe is None or canonical_probe.probe_hash != job.probe_hash:
        return JobStage.PROBE
    if job.plan_path is None or job.plan_hash is None or not _plan_artifact_valid(job):
        return JobStage.PLAN
    plan = _load_job_plan(job)
    if (
        plan.profile_hash != identity.profile_hash
        or plan.vapoursynth.identity_hash != identity.vapoursynth_identity_hash
        or plan.execution_identity.identity_hash != identity.execution_identity_hash
    ):
        return JobStage.PLAN
    validation = latest_validation(session, job)
    if _output_exists(job):
        if validation is not None and validation.passed:
            if has_completed_promotion(session, job):
                return None
            return JobStage.PROMOTE
        return JobStage.VALIDATE
    return JobStage.ENCODE


def _output_exists(job: Job) -> bool:
    return job.output_path is not None and Path(job.output_path).is_file()


def _encoded_output_exists(job: Job) -> bool:
    return job.output_path is not None and Path(job.output_path).exists()


def _scheduler_error(error: Exception) -> Exception:
    if isinstance(error, SchedulerError | ProbeError):
        return error
    return JobPreparationError(str(error))


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


def _source_media_file(session: Session, media_file_id: int) -> MediaFile:
    try:
        return require_inventory_media_file(session, media_file_id=media_file_id)
    except MediaFileNotFoundError as exc:
        raise JobPreparationError(str(exc)) from exc


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
