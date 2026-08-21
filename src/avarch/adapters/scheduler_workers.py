from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import Any

from sqlalchemy import Engine
from sqlmodel import Session

from avarch.adapters.execution import build_av1an_command, execute_plan, should_resume_av1an
from avarch.adapters.filesystem.plans import PlanArtifactConflictError, write_plan_artifacts
from avarch.adapters.filesystem.scanner import create_file_snapshot
from avarch.adapters.job_preparation import (
    JobPreparationError,
    StaleJobProfileError,
    StaleJobSourceError,
    config_data_dir,
    load_job_plan,
    require_id,
    require_profile,
    scheduler_error,
    snapshot_job,
    source_media_file,
    verify_job_profile,
    verify_media_snapshot,
)
from avarch.adapters.probe import build_ffprobe_command, normalize_probe, run_ffprobe
from avarch.adapters.sqlite import job_transitions as job_transition_adapter
from avarch.adapters.sqlite.db import create_db_engine
from avarch.adapters.sqlite.job_transitions import (
    cancel_claimed_job,
    claim_job_stage,
    clear_hold_fields,
    complete_job_stage,
    complete_scene_detect_stage,
    fail_job_stage,
    interrupt_job_stage,
    require_attempt,
    require_job,
    restore_missing_scene_detect_stage,
)
from avarch.adapters.sqlite.models import Job, MediaFile, ProbeResult, ValidationResult
from avarch.adapters.sqlite.planning import load_planning_context
from avarch.adapters.sqlite.probes import store_probe_result
from avarch.adapters.sqlite.progress import SqliteProgressStore
from avarch.adapters.sqlite.queue import (
    plan_hash_conflicts_with_other_job,
    queue_key_conflicts_with_other_job,
)
from avarch.adapters.sqlite.rejection_cleanup import (
    RejectedOutputCleanupError,
    cleanup_rejected_output,
)
from avarch.adapters.sqlite.stage_events import record_stage_event
from avarch.adapters.sqlite.validations import latest_validation, persist_validation_result
from avarch.adapters.validation import ValidationError as OutputValidationError
from avarch.adapters.validation import (
    validate_output,
)
from avarch.adapters.vapoursynth import (
    VapourSynthGenerationError,
    generate_vapoursynth_script,
    validate_script_syntax,
)
from avarch.adapters.vpy_env import planning_runtime_identity_for_data_dir
from avarch.application.planning import PlanningError, build_plan, match_profile
from avarch.application.progress import (
    CoalescingProgressBridge,
    ProgressPersistenceThrottle,
    ProgressSink,
    publish_progress_safely,
)
from avarch.application.promotion import PromotionWorkflow, promote_job, recover_promotion
from avarch.application.queue_identity import build_queue_key, planning_identity
from avarch.application.validation_summary import failed_check_summary, failed_required_check_names
from avarch.application.vapoursynth_identity import (
    resolve_vapoursynth_filter,
    resolve_vapoursynth_template,
)
from avarch.config import AppConfig
from avarch.domain.jobs import (
    AttemptStatus,
    JobEventType,
    JobStage,
    JobStatus,
    job_has_passed_validation,
)
from avarch.domain.progress import ProgressPhase, ProgressSnapshot, ProgressSource
from avarch.domain.size import SizeDecision, SizePolicy, evaluate_size_policy
from avarch.models.execution import (
    ExecutionError,
    ExecutionInterruptedError,
    ProcessCancellationToken,
)
from avarch.models.plan import TranscodePlan
from avarch.models.validation import ValidationReport
from avarch.serialization import canonical_json


class _ProgressFanoutSink:
    def __init__(self, sinks: tuple[ProgressSink, ...]) -> None:
        self._sinks = sinks

    def publish(self, snapshot: ProgressSnapshot) -> None:
        for sink in self._sinks:
            publish_progress_safely(sink, snapshot)


class _SceneDetectStageProgressSink:
    def __init__(
        self,
        *,
        engine: Engine,
        job_id: int,
        attempt_id: int,
        downstream: ProgressSink,
        scene_completed: bool = False,
    ) -> None:
        self._engine = engine
        self._job_id = job_id
        self._attempt_id = attempt_id
        self._downstream = downstream
        self._scene_started_at: datetime | None = None
        self._scenes_found: int | None = None
        self._chunks_prepared: int | None = None
        self._completed = scene_completed
        self._lock = Lock()

    def publish(self, snapshot: ProgressSnapshot) -> None:
        if snapshot.phase == ProgressPhase.SCENE_DETECTION:
            if self._is_completed():
                if snapshot.source == ProgressSource.PROCESS_HEARTBEAT:
                    publish_progress_safely(self._downstream, _encoding_heartbeat(snapshot))
                return
            self._record_scene_progress(snapshot)
        elif snapshot.phase == ProgressPhase.ENCODING:
            self._complete_scene_detect(snapshot.observed_at)
        publish_progress_safely(self._downstream, snapshot)

    def complete_if_needed(self, *, now: datetime) -> None:
        self._complete_scene_detect(now)

    def _record_scene_progress(self, snapshot: ProgressSnapshot) -> None:
        with self._lock:
            self._scene_started_at = self._scene_started_at or snapshot.phase_started_at
            self._scenes_found = _scenes_found_from_message(snapshot.message) or self._scenes_found
            self._chunks_prepared = (
                _chunks_prepared_from_message(snapshot.message) or self._chunks_prepared
            )

    def _is_completed(self) -> bool:
        with self._lock:
            return self._completed

    def _complete_scene_detect(self, now: datetime) -> None:
        with self._lock:
            if self._completed:
                return
            started_at = self._scene_started_at
            scenes_found = self._scenes_found
            chunks_prepared = self._chunks_prepared
            duration_seconds = (
                round((now - started_at).total_seconds(), 1) if started_at is not None else None
            )
            with Session(self._engine) as session, session.begin():
                complete_scene_detect_stage(
                    session,
                    job_id=self._job_id,
                    attempt_id=self._attempt_id,
                    scenes_found=scenes_found,
                    chunks_prepared=chunks_prepared,
                    duration_seconds=duration_seconds,
                    now=now,
                )
            self._completed = True


def _scenes_found_from_message(message: str | None) -> int | None:
    if message is None:
        return None
    match = re.search(r"\bscenes found:\s*(\d[\d,]*)\b", message, re.IGNORECASE)
    if match is None:
        return None
    return int(match.group(1).replace(",", ""))


def _chunks_prepared_from_message(message: str | None) -> int | None:
    if message is None:
        return None
    match = re.search(r"\bchunks prepared:\s*(\d[\d,]*)\b", message, re.IGNORECASE)
    if match is None:
        return None
    return int(match.group(1).replace(",", ""))


def _restore_missing_scene_detect_stage(session: Session, *, job: Job) -> bool:
    return restore_missing_scene_detect_stage(session, job=job)


def _encoding_heartbeat(snapshot: ProgressSnapshot) -> ProgressSnapshot:
    return replace(
        snapshot,
        phase=ProgressPhase.ENCODING,
        current=None,
        total=None,
        unit=None,
        rate_per_second=None,
        speed_ratio=None,
        message="telemetry pending",
        chunks_current=None,
        chunks_total=None,
        bitrate_kbps=None,
        estimated_output_bytes=None,
        written_output_bytes=None,
        advanced_at=None,
    )


async def execute_probe_job(
    *,
    job_id: int,
    runner_id: str,
    config: AppConfig,
) -> None:
    engine = create_db_engine(config.database.url)
    now = _utc_now()
    with Session(engine) as session, session.begin():
        job = require_job(session, job_id)
        media_file = source_media_file(session, job.media_file_id)
        attempt = claim_job_stage(session, job_id=job_id, runner_id=runner_id, now=now)
        attempt.command_json = canonical_json(build_ffprobe_command(Path(media_file.path)))
        attempt_id = require_id(attempt)
        media_path = Path(media_file.path)
        try:
            verify_media_snapshot(media_file, expected_fingerprint=job.source_fs_fingerprint)
        except Exception as exc:
            fail_job_stage(
                session,
                job_id=job_id,
                attempt_id=attempt_id,
                error=scheduler_error(exc),
                exit_code=None,
                now=_utc_now(),
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
                error=scheduler_error(exc),
                exit_code=None,
                now=_utc_now(),
            )
        return

    with Session(engine) as session, session.begin():
        job = require_job(session, job_id)
        media_file = source_media_file(session, job.media_file_id)
        probe_result = store_probe_result(
            session,
            media_file=media_file,
            raw_probe=raw_probe,
            normalized_probe=normalized_probe,
            created_at=_utc_now(),
        )
        session.flush()
        _attach_probe_and_advance(
            session,
            job=job,
            media_file=media_file,
            probe_result=probe_result,
            config=config,
            attempt_id=attempt_id,
            now=_utc_now(),
        )


async def execute_plan_job(
    *,
    job_id: int,
    runner_id: str,
    config: AppConfig,
) -> None:
    engine = create_db_engine(config.database.url)
    now = _utc_now()
    with Session(engine) as session, session.begin():
        job = require_job(session, job_id)
        media_file = source_media_file(session, job.media_file_id)
        attempt = claim_job_stage(session, job_id=job_id, runner_id=runner_id, now=now)
        attempt_id = require_id(attempt)
        input_path = Path(media_file.path)
        try:
            verify_media_snapshot(media_file, expected_fingerprint=job.source_fs_fingerprint)
        except Exception as exc:
            fail_job_stage(
                session,
                job_id=job_id,
                attempt_id=attempt_id,
                error=scheduler_error(exc),
                exit_code=None,
                now=_utc_now(),
            )
            return

    try:
        with Session(engine) as session:
            job = require_job(session, job_id)
            resolved_profile = require_profile(config, job.profile_name)
            verify_job_profile(config, job)
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
                    now=_utc_now(),
                )
                return
            resolved_template = resolve_vapoursynth_template(context.profile)
            resolved_filter = resolve_vapoursynth_filter(context.profile)
            plan = build_plan(
                context,
                data_dir=config_data_dir(config),
                runtime_identity=planning_runtime_identity_for_data_dir(config_data_dir(config)),
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
            now=_utc_now(),
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
            now=_utc_now(),
        )
        return
    except Exception as exc:
        job_transition_adapter.fail_job_after_external(
            engine,
            job_id=job_id,
            attempt_id=attempt_id,
            error=exc,
            now=_utc_now(),
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
                now=_utc_now(),
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
            next_stage=JobStage.SCENE_DETECT,
            now=_utc_now(),
        )


async def execute_encode_job(
    *,
    job_id: int,
    runner_id: str,
    config: AppConfig,
    progress_sink: ProgressSink | None = None,
) -> None:
    engine = create_db_engine(config.database.url)
    now = _utc_now()
    with Session(engine) as session, session.begin():
        job = require_job(session, job_id)
        scene_completed = _restore_missing_scene_detect_stage(session, job=job)
        initial_stage = JobStage(job.stage)
        media_file = source_media_file(session, job.media_file_id)
        attempt = claim_job_stage(session, job_id=job_id, runner_id=runner_id, now=now)
        attempt_id = require_id(attempt)
        try:
            verify_media_snapshot(media_file, expected_fingerprint=job.source_fs_fingerprint)
            plan = load_job_plan(job)
            if plan.plan_hash != job.plan_hash:
                raise JobPreparationError("Stored plan hash does not match the queued job.")
            effective_resume = should_resume_av1an(plan.av1an)
        except Exception as exc:
            fail_job_stage(
                session,
                job_id=job_id,
                attempt_id=attempt_id,
                error=scheduler_error(exc),
                exit_code=None,
                now=_utc_now(),
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

    preparing = _phase_snapshot(ProgressPhase.PREPARING, now=now)
    await _persist_attempt_progress(engine, attempt_id=attempt_id, snapshot=preparing)
    publish_progress_safely(progress_sink, preparing)
    progress_bridge = CoalescingProgressBridge(
        lambda snapshot: _persist_attempt_progress(
            engine,
            attempt_id=attempt_id,
            snapshot=snapshot,
        )
    )
    persisted_progress_sink = ProgressPersistenceThrottle(progress_bridge)
    execution_progress_sink: ProgressSink = _ProgressFanoutSink(
        (persisted_progress_sink, progress_sink)
        if progress_sink is not None
        else (persisted_progress_sink,)
    )
    scene_detect_sink: _SceneDetectStageProgressSink | None = None
    if initial_stage in {JobStage.SCENE_DETECT, JobStage.ENCODE}:
        scene_detect_sink = _SceneDetectStageProgressSink(
            engine=engine,
            job_id=job_id,
            attempt_id=attempt_id,
            downstream=execution_progress_sink,
            scene_completed=scene_completed,
        )
        execution_progress_sink = scene_detect_sink
    process_cancellation = ProcessCancellationToken()
    execution_task = asyncio.create_task(
        asyncio.to_thread(
            execute_plan,
            plan,
            cancellation_token=process_cancellation,
            progress_sink=execution_progress_sink,
        )
    )
    try:
        status = await asyncio.shield(execution_task)
    except asyncio.CancelledError:
        process_cancellation.request("scheduler cancellation")
        try:
            await asyncio.shield(execution_task)
        except ExecutionInterruptedError:
            pass
        except ExecutionError:
            pass
        await _persist_attempt_progress(
            engine,
            attempt_id=attempt_id,
            snapshot=_phase_snapshot(
                ProgressPhase.CANCELLED,
                now=_utc_now(),
                message="scheduler cancellation",
            ),
        )
        with Session(engine) as session, session.begin():
            interrupt_job_stage(session, job_id=job_id, attempt_id=attempt_id, now=_utc_now())
        return
    except ExecutionInterruptedError:
        await _persist_attempt_progress(
            engine,
            attempt_id=attempt_id,
            snapshot=_phase_snapshot(
                ProgressPhase.CANCELLED,
                now=_utc_now(),
                message="execution interrupted",
            ),
        )
        with Session(engine) as session, session.begin():
            interrupt_job_stage(session, job_id=job_id, attempt_id=attempt_id, now=_utc_now())
        return
    except ExecutionError as exc:
        await _persist_attempt_progress(
            engine,
            attempt_id=attempt_id,
            snapshot=_phase_snapshot(
                ProgressPhase.FAILED,
                now=_utc_now(),
                message=str(exc),
            ),
        )
        job_transition_adapter.fail_job_after_external(
            engine,
            job_id=job_id,
            attempt_id=attempt_id,
            error=exc,
            now=_utc_now(),
        )
        return
    finally:
        persisted_progress_sink.close()
        await progress_bridge.aclose()

    completed = _phase_snapshot(ProgressPhase.COMPLETED, now=_utc_now())
    await _persist_attempt_progress(engine, attempt_id=attempt_id, snapshot=completed)
    publish_progress_safely(progress_sink, completed)
    if scene_detect_sink is not None:
        scene_detect_sink.complete_if_needed(now=_utc_now())
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
            now=_utc_now(),
        )


async def execute_validation_job(
    *,
    job_id: int,
    runner_id: str,
    config: AppConfig,
) -> ValidationResult | None:
    engine = create_db_engine(config.database.url)
    now = _utc_now()
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
        attempt_id = require_id(attempt)
        try:
            plan = load_job_plan(job)
            if plan.plan_hash != job.plan_hash:
                raise JobPreparationError("Stored plan hash does not match the queued job.")
            validation_job = snapshot_job(job)
        except Exception as exc:
            fail_job_stage(
                session,
                job_id=job_id,
                attempt_id=attempt_id,
                error=scheduler_error(exc),
                exit_code=None,
                now=_utc_now(),
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

    await _persist_attempt_progress(
        engine,
        attempt_id=attempt_id,
        snapshot=_phase_snapshot(ProgressPhase.VALIDATING, now=now),
    )
    try:
        report = await validate_output(
            job=validation_job,
            plan=plan,
            policy=plan.validation,
            runtime_paths=plan.runtime,
            clock=_utc_now,
        )
    except OutputValidationError as exc:
        await _persist_attempt_progress(
            engine,
            attempt_id=attempt_id,
            snapshot=_phase_snapshot(
                ProgressPhase.FAILED,
                now=_utc_now(),
                message=str(exc),
            ),
        )
        job_transition_adapter.fail_job_after_external(
            engine,
            job_id=job_id,
            attempt_id=attempt_id,
            error=exc,
            now=_utc_now(),
        )
        return None
    except Exception as exc:
        await _persist_attempt_progress(
            engine,
            attempt_id=attempt_id,
            snapshot=_phase_snapshot(
                ProgressPhase.FAILED,
                now=_utc_now(),
                message=str(exc),
            ),
        )
        job_transition_adapter.fail_job_after_external(
            engine,
            job_id=job_id,
            attempt_id=attempt_id,
            error=exc,
            now=_utc_now(),
        )
        return None

    with Session(engine) as session, session.begin():
        job = require_job(session, job_id)
        if job.cancel_requested_at is not None:
            attempt = require_attempt(session, attempt_id)
            cancel_claimed_job(session, job=job, attempt=attempt, now=_utc_now())
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
        size_decision = None
        if report.passed:
            size_decision = _apply_size_policy_after_validation(
                job=job,
                plan=plan,
                report=report,
                config=config,
                now=_utc_now(),
            )
        record_stage_event(
            session,
            job_id=job_id,
            attempt=attempt,
            event_type=JobEventType.STAGE_COMPLETED
            if report.passed
            else JobEventType.STAGE_FAILED,
            now=report.finished_at,
            details=_validation_event_details(
                result_id=result.id,
                report=report,
                failed_checks=failed_checks,
                size_decision=size_decision,
                source_size_bytes=plan.validation.source_size_bytes,
            ),
        )
        if report.passed and job.hold_requested_at is not None:
            clear_hold_fields(job)
        if report.passed:
            SqliteProgressStore(session).finalize_snapshot(
                attempt_id=attempt_id,
                snapshot=_phase_snapshot(ProgressPhase.COMPLETED, now=report.finished_at),
                persisted_at=report.finished_at,
            )
        return result


async def execute_promotion_job(
    *,
    job_id: int,
    runner_id: str,
    config: AppConfig,
    promotion_workflow: PromotionWorkflow,
) -> None:
    engine = create_db_engine(config.database.url)
    with Session(engine) as session:
        job = require_job(session, job_id)
        recovering = job.status == JobStatus.PROMOTING
    if recovering:
        await recover_promotion(
            workflow=promotion_workflow,
            job_id=job_id,
            config=config,
            owner_token=runner_id,
        )
        return
    await promote_job(
        workflow=promotion_workflow,
        job_id=job_id,
        config=config,
        owner_token=runner_id,
    )


async def execute_cleanup_job(
    *,
    job_id: int,
    runner_id: str,
    config: AppConfig,
) -> None:
    engine = create_db_engine(config.database.url)
    now = _utc_now()
    with Session(engine) as session, session.begin():
        job = require_job(session, job_id)
        attempt = claim_job_stage(session, job_id=job_id, runner_id=runner_id, now=now)
        attempt_id = require_id(attempt)
        try:
            plan = load_job_plan(job)
            validation = latest_validation(session, job)
            if validation is None or not validation.passed:
                raise JobPreparationError("Cleanup requires a passing validation result.")
            report = ValidationReport.model_validate_json(validation.details_json)
            profile = require_profile(config, job.profile_name).profile
            decision = _size_policy_decision(profile=profile, plan=plan, report=report)
            attempt.output_path = validation.output_path
            attempt.details_json = canonical_json(
                {
                    "validation_result_id": validation.id,
                    "size_decision": decision.value,
                    "output_path": validation.output_path,
                }
            )
            if decision == SizeDecision.ACCEPT:
                complete_job_stage(
                    session,
                    job_id=job_id,
                    attempt_id=attempt_id,
                    next_stage=JobStage.PROMOTE,
                    now=_utc_now(),
                )
                return
            try:
                cleanup_rejected_output(
                    job,
                    encoded_path=Path(validation.output_path),
                    decision=decision,
                    profile=profile,
                    now=_utc_now(),
                )
            except RejectedOutputCleanupError as exc:
                attempt.status = AttemptStatus.FAILED
                attempt.error_type = exc.__class__.__name__
                attempt.error_message = str(exc)
                attempt.finished_at = _utc_now()
                record_stage_event(
                    session,
                    job_id=job_id,
                    attempt=attempt,
                    event_type=JobEventType.STAGE_FAILED,
                    now=attempt.finished_at,
                    details=_cleanup_event_details(
                        decision=decision,
                        plan=plan,
                        report=report,
                        output_path=validation.output_path,
                        error_type=attempt.error_type,
                    ),
                )
                session.add(job)
                session.add(attempt)
                return
            attempt.status = AttemptStatus.COMPLETED
            attempt.finished_at = _utc_now()
            record_stage_event(
                session,
                job_id=job_id,
                attempt=attempt,
                event_type=JobEventType.STAGE_COMPLETED,
                now=attempt.finished_at,
                details=_cleanup_event_details(
                    decision=decision,
                    plan=plan,
                    report=report,
                    output_path=validation.output_path,
                ),
            )
            session.add(job)
            session.add(attempt)
        except Exception as exc:
            fail_job_stage(
                session,
                job_id=job_id,
                attempt_id=attempt_id,
                error=scheduler_error(exc),
                exit_code=None,
                now=_utc_now(),
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
    identity = planning_identity(require_profile(config, job.profile_name))
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


def _apply_size_policy_after_validation(
    *,
    job: Job,
    plan: TranscodePlan,
    report: Any,
    config: AppConfig,
    now: datetime,
) -> SizeDecision:
    profile = require_profile(config, job.profile_name).profile
    decision = _size_policy_decision(profile=profile, plan=plan, report=report)
    if decision == SizeDecision.ACCEPT:
        return decision
    job_transition_adapter.queue_rejected_output_cleanup(job, now=now)
    return decision


def _size_policy_decision(
    *,
    profile: Any,
    plan: TranscodePlan,
    report: Any,
) -> SizeDecision:
    encoded_size = (
        report.observed.output_size_bytes
        if report.observed is not None and report.observed.output_size_bytes is not None
        else Path(report.output_path).stat().st_size
    )
    return evaluate_size_policy(
        plan.validation.source_size_bytes,
        encoded_size,
        SizePolicy(
            require_smaller=profile.promotion.require_smaller,
            minimum_savings_percent=profile.promotion.minimum_savings_percent,
        ),
    )


def _validation_event_details(
    *,
    result_id: int | None,
    report: ValidationReport,
    failed_checks: list[str],
    size_decision: SizeDecision | None,
    source_size_bytes: int,
) -> dict[str, object | None]:
    output_size_bytes = _observed_output_size(report)
    return {
        "validation_result_id": result_id,
        "passed": report.passed,
        "failed_checks": list(failed_checks),
        "source_size_bytes": source_size_bytes,
        "output_size_bytes": output_size_bytes,
        "saved_bytes": source_size_bytes - output_size_bytes
        if output_size_bytes is not None
        else None,
        "size_decision": size_decision.value if size_decision is not None else None,
        "source_safety_outcome": "original_retained",
    }


def _cleanup_event_details(
    *,
    decision: SizeDecision,
    plan: TranscodePlan,
    report: ValidationReport,
    output_path: str,
    error_type: str | None = None,
) -> dict[str, object | None]:
    output_size_bytes = _observed_output_size(report)
    return {
        "size_decision": decision.value,
        "source_size_bytes": plan.validation.source_size_bytes,
        "output_size_bytes": output_size_bytes,
        "saved_bytes": plan.validation.source_size_bytes - output_size_bytes
        if output_size_bytes is not None
        else None,
        "output_path": output_path,
        "source_safety_outcome": "original_retained",
        "error_type": error_type,
    }


def _observed_output_size(report: ValidationReport) -> int | None:
    if report.observed is not None and report.observed.output_size_bytes is not None:
        return report.observed.output_size_bytes
    try:
        return Path(report.output_path).stat().st_size
    except OSError:
        return None


def _phase_snapshot(
    phase: ProgressPhase,
    *,
    now: datetime,
    message: str | None = None,
) -> ProgressSnapshot:
    return ProgressSnapshot(
        phase=phase,
        current=None,
        total=None,
        unit=None,
        rate_per_second=None,
        speed_ratio=None,
        source=ProgressSource.SCHEDULER,
        message=_sanitize_progress_message(message),
        phase_started_at=now,
        observed_at=now,
        heartbeat_at=now,
        advanced_at=None,
    )


async def _persist_attempt_progress(
    engine: Engine,
    *,
    attempt_id: int,
    snapshot: ProgressSnapshot,
) -> None:
    await asyncio.to_thread(_save_attempt_progress, engine, attempt_id, snapshot)


def _save_attempt_progress(
    engine: Engine,
    attempt_id: int,
    snapshot: ProgressSnapshot,
) -> None:
    snapshot = _snapshot_with_sanitized_message(snapshot)
    with Session(engine) as session:
        store = SqliteProgressStore(session)
        if snapshot.phase in {
            ProgressPhase.COMPLETED,
            ProgressPhase.FAILED,
            ProgressPhase.CANCELLED,
        }:
            store.finalize_snapshot(
                attempt_id=attempt_id,
                snapshot=snapshot,
                persisted_at=snapshot.observed_at,
            )
        else:
            store.save_snapshot(
                attempt_id=attempt_id,
                snapshot=snapshot,
                persisted_at=snapshot.observed_at,
            )
        session.commit()


ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
CONTROL_CHARACTER_PATTERN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _snapshot_with_sanitized_message(snapshot: ProgressSnapshot) -> ProgressSnapshot:
    sanitized = _sanitize_progress_message(snapshot.message)
    if sanitized == snapshot.message:
        return snapshot
    return ProgressSnapshot(
        phase=snapshot.phase,
        current=snapshot.current,
        total=snapshot.total,
        unit=snapshot.unit,
        rate_per_second=snapshot.rate_per_second,
        speed_ratio=snapshot.speed_ratio,
        source=snapshot.source,
        message=sanitized,
        phase_started_at=snapshot.phase_started_at,
        observed_at=snapshot.observed_at,
        heartbeat_at=snapshot.heartbeat_at,
        advanced_at=snapshot.advanced_at,
    )


def _sanitize_progress_message(message: str | None, *, max_length: int = 500) -> str | None:
    if message is None:
        return None
    without_ansi = ANSI_ESCAPE_PATTERN.sub("", message)
    without_control = CONTROL_CHARACTER_PATTERN.sub(" ", without_ansi)
    sanitized = " ".join(without_control.split())
    if len(sanitized) <= max_length:
        return sanitized
    return sanitized[: max_length - 3].rstrip() + "..."


def _utc_now() -> datetime:
    from datetime import UTC, datetime

    return datetime.now(UTC)
