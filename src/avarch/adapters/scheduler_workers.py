from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, replace
from datetime import datetime
from fractions import Fraction
from pathlib import Path
from threading import Lock, Thread
from typing import Any

from sqlalchemy import Engine
from sqlmodel import Session

from avarch.adapters.calibration import ManagedCalibrationProcessRunner
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
from avarch.adapters.sqlite.performance import (
    CompatibleCalibrationObservation,
    CompatiblePerformanceObservation,
    compatible_calibration_observations,
    compatible_performance_observations,
    persist_calibration_observation,
    persist_performance_observation,
)
from avarch.adapters.sqlite.planning import load_planning_context
from avarch.adapters.sqlite.probes import get_canonical_probe_result, store_probe_result
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
from avarch.application.attempt_metrics import AttemptMetricsAccumulator, AttemptMetricsSummary
from avarch.application.calibration_orchestrator import (
    CalibrationExecutionResult,
    CalibrationStatus,
    MeasuredResourceSelection,
    run_calibration,
)
from avarch.application.candidate_tournament import CandidateKey, candidate_key
from avarch.application.environment_signature import (
    ExecutionEnvironmentSignature,
    build_execution_environment_signature,
)
from avarch.application.history_estimator import estimate_candidates_from_history
from avarch.application.performance_candidates import (
    CandidateSvtLpValue,
    CandidateWorkerValue,
    PerformanceCandidate,
    generate_safe_concurrency_candidates,
)
from avarch.application.performance_selector import select_estimated_candidate
from avarch.application.planning import PlanningError, build_plan, match_profile
from avarch.application.probe_summary import parse_normalized_probe_json
from avarch.application.progress import (
    CoalescingProgressBridge,
    ProgressPersistenceThrottle,
    ProgressSink,
    publish_progress_safely,
)
from avarch.application.promotion import PromotionWorkflow, promote_job, recover_promotion
from avarch.application.queue_identity import build_queue_key, planning_identity
from avarch.application.resource_decision import (
    ExecutionResourceDecision,
    ResourceDecisionService,
)
from avarch.application.resource_limits import parse_memory_reserve, resolve_resource_limits
from avarch.application.resources import ResourceSnapshot, effective_resource_snapshot
from avarch.application.validation_summary import failed_check_summary, failed_required_check_names
from avarch.application.vapoursynth_identity import (
    resolve_vapoursynth_filter,
    resolve_vapoursynth_template,
)
from avarch.application.workload_signature import WorkloadSignature, build_workload_signature
from avarch.config import AppConfig, PerformanceSettings
from avarch.domain.encoder_args import (
    LP_OPTIONS,
    normalize_svt_operational_args,
    svt_lp_definitions,
)
from avarch.domain.jobs import (
    AttemptStatus,
    JobEventType,
    JobStage,
    JobStatus,
    job_has_passed_validation,
)
from avarch.domain.progress import ProgressPhase, ProgressSnapshot, ProgressSource
from avarch.domain.resource_policy import parse_resource_intent
from avarch.domain.scheduler import JobResourceReservation, ResourceCapacity
from avarch.domain.size import SizeDecision, SizePolicy, evaluate_size_policy
from avarch.models.execution import (
    ExecutionError,
    ExecutionInterruptedError,
    ProcessCancellationToken,
    ProcessResourceSummary,
    ResourceExhaustionError,
)
from avarch.models.plan import EncodeExecutionStatus, TranscodePlan
from avarch.models.validation import ValidationReport
from avarch.serialization import canonical_json


@dataclass(frozen=True, slots=True)
class _ExecutionOutcome:
    status: EncodeExecutionStatus | None = None
    error_kind: str | None = None
    message: str = ""


@dataclass(slots=True)
class _ExecutionThread:
    thread: Thread
    outcomes: list[_ExecutionOutcome]

    @classmethod
    def start(
        cls,
        plan: TranscodePlan,
        *,
        cancellation_token: ProcessCancellationToken,
        progress_sink: ProgressSink,
    ) -> _ExecutionThread:
        outcomes: list[_ExecutionOutcome] = []

        def target() -> None:
            outcomes.append(
                _execute_plan_outcome(
                    plan,
                    cancellation_token=cancellation_token,
                    progress_sink=progress_sink,
                )
            )

        thread = Thread(target=target, name="avarch-encode-execution", daemon=True)
        thread.start()
        return cls(thread=thread, outcomes=outcomes)

    @property
    def done(self) -> bool:
        return not self.thread.is_alive()

    def outcome(self) -> _ExecutionOutcome:
        if not self.done:
            raise RuntimeError("encode execution thread is still running")
        if self.outcomes:
            return self.outcomes[0]
        return _ExecutionOutcome(
            error_kind="unexpected_error",
            message="encode execution thread exited without an outcome",
        )


@dataclass(slots=True)
class _CalibrationThread:
    thread: Thread
    results: list[CalibrationExecutionResult]
    errors: list[BaseException]

    @classmethod
    def start(cls, **kwargs: Any) -> _CalibrationThread:
        results: list[CalibrationExecutionResult] = []
        errors: list[BaseException] = []

        def target() -> None:
            try:
                results.append(run_calibration(**kwargs))
            except BaseException as exc:
                errors.append(exc)

        thread = Thread(target=target, name="avarch-calibration", daemon=True)
        thread.start()
        return cls(thread=thread, results=results, errors=errors)

    @property
    def done(self) -> bool:
        return not self.thread.is_alive()

    def result(self) -> CalibrationExecutionResult:
        if not self.done:
            raise RuntimeError("calibration thread is still running")
        if self.errors:
            raise self.errors[0]
        if not self.results:
            raise RuntimeError("calibration thread exited without a result")
        return self.results[0]


class _ProgressFanoutSink:
    def __init__(self, sinks: tuple[ProgressSink, ...]) -> None:
        self._sinks = sinks

    def publish(self, snapshot: ProgressSnapshot) -> None:
        for sink in self._sinks:
            publish_progress_safely(sink, snapshot)

    def record_resource_summary(self, summary: ProcessResourceSummary) -> None:
        for sink in self._sinks:
            callback = getattr(sink, "record_resource_summary", None)
            if callable(callback):
                callback(summary)


class _AttemptMetricsProgressSink:
    def __init__(self) -> None:
        self.accumulator = AttemptMetricsAccumulator()

    def publish(self, snapshot: ProgressSnapshot) -> None:
        self.accumulator.record_progress(snapshot)

    def record_resource_summary(self, summary: ProcessResourceSummary) -> None:
        self.accumulator.record_resource_summary(summary)


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

    def record_resource_summary(self, summary: ProcessResourceSummary) -> None:
        callback = getattr(self._downstream, "record_resource_summary", None)
        if callable(callback):
            callback(summary)

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
    reservation: JobResourceReservation | None = None,
    progress_sink: ProgressSink | None = None,
) -> None:
    engine = create_db_engine(config.database.url)
    now = _utc_now()
    with Session(engine) as session, session.begin():
        job = require_job(session, job_id)
        scene_completed = _restore_missing_scene_detect_stage(session, job=job)
        initial_stage = JobStage(job.stage)
        media_file = source_media_file(session, job.media_file_id)
        attempt = claim_job_stage(
            session,
            job_id=job_id,
            runner_id=runner_id,
            now=now,
            reservation=reservation,
        )
        attempt_id = require_id(attempt)
        try:
            verify_media_snapshot(media_file, expected_fingerprint=job.source_fs_fingerprint)
            plan = load_job_plan(job)
            if plan.plan_hash != job.plan_hash:
                raise JobPreparationError("Stored plan hash does not match the queued job.")
            effective_resume = should_resume_av1an(plan.av1an)
            frame_rate = _source_frame_rate(
                session,
                media_file=media_file,
                stream_index=plan.video.source_stream_index,
            )
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
    resource_snapshot = effective_resource_snapshot()
    environment_signature = build_execution_environment_signature(
        snapshot=resource_snapshot,
        tool_versions=_tool_versions_payload(plan),
    )
    workload_signature = build_workload_signature(plan)
    capacity = _selection_capacity(
        snapshot=resource_snapshot,
        reservation=reservation,
        config=config,
    )
    intent = parse_resource_intent(
        workers=plan.av1an.workers,
        svt_lp=_svt_lp_from_encoder_args(plan.av1an.encoder_args),
    )
    with Session(engine) as session:
        observations = compatible_performance_observations(
            session,
            environment_signature_hash=environment_signature.signature_hash,
            workload_signature_hash=workload_signature.signature_hash,
            semantic_hash=plan.semantic_hash,
            limit=20,
        )
        calibrations = compatible_calibration_observations(
            session,
            environment_signature_hash=environment_signature.signature_hash,
            workload_signature_hash=workload_signature.signature_hash,
            semantic_hash=plan.semantic_hash,
            limit=5,
        )

    preparing = _phase_snapshot(
        ProgressPhase.PREPARING,
        now=_utc_now(),
        message="selecting encode resources",
    )
    await _persist_attempt_progress(engine, attempt_id=attempt_id, snapshot=preparing)
    publish_progress_safely(progress_sink, preparing)

    candidates = generate_safe_concurrency_candidates(
        intent=intent,
        capacity=capacity,
        observations=tuple(observations),
        max_candidates=config.performance.calibration_max_candidates,
    )
    calibration_key = _calibration_key(
        plan=plan,
        environment_signature=environment_signature,
        workload_signature=workload_signature,
        candidates=candidates,
        settings=config.performance,
    )
    calibration_selection = _selection_from_calibration(
        calibrations=tuple(calibrations),
        candidates=candidates,
        calibration_key=calibration_key,
    )
    historical_decision = _resolve_encode_resource_decision(
        plan,
        snapshot=resource_snapshot,
        capacity=capacity,
        observations=tuple(observations),
    )
    calibration_result: CalibrationExecutionResult | None = None
    if calibration_selection is not None:
        resource_decision = ResourceDecisionService().resolve(
            intent=intent,
            snapshot=resource_snapshot,
            evidence=calibration_selection,
        )
    elif not historical_decision.fallback:
        resource_decision = historical_decision
    else:
        calibration_cancellation = ProcessCancellationToken()

        def calibration_status(message: str) -> None:
            snapshot = _phase_snapshot(
                ProgressPhase.PREPARING,
                now=_utc_now(),
                message=message,
            )
            _save_attempt_progress(engine, attempt_id, snapshot)
            publish_progress_safely(progress_sink, snapshot)

        calibration_thread = _CalibrationThread.start(
            plan=plan,
            settings=config.performance,
            intent=intent,
            capacity=capacity,
            observations=tuple(observations),
            frame_rate=frame_rate,
            predicted_encode_seconds=_predicted_encode_seconds(
                plan=plan,
                frame_rate=frame_rate,
                observations=tuple(observations),
            ),
            calibration_dir=plan.temp_dir / "calibration" / calibration_key[:16],
            runner=ManagedCalibrationProcessRunner(plan_hash=plan.plan_hash),
            command_renderer=build_av1an_command,
            cancellation_token=calibration_cancellation,
            status_sink=calibration_status,
        )
        try:
            calibration_result = await _wait_calibration_thread(calibration_thread)
        except asyncio.CancelledError:
            calibration_cancellation.request("scheduler cancellation")
            await _wait_calibration_thread(calibration_thread)
            _save_attempt_progress(
                engine,
                attempt_id,
                _phase_snapshot(
                    ProgressPhase.CANCELLED,
                    now=_utc_now(),
                    message="scheduler cancellation during calibration",
                ),
            )
            with Session(engine) as session, session.begin():
                interrupt_job_stage(session, job_id=job_id, attempt_id=attempt_id, now=_utc_now())
            return
        _persist_calibration_result_safely(
            engine=engine,
            plan=plan,
            environment_signature=environment_signature,
            workload_signature=workload_signature,
            candidates=candidates,
            settings=config.performance,
            result=calibration_result,
            now=_utc_now(),
        )
        if calibration_result.status in {
            CalibrationStatus.CANCELLED,
            CalibrationStatus.INCOMPLETE,
        }:
            message = _calibration_incomplete_message(calibration_result)
            _save_attempt_progress(
                engine,
                attempt_id,
                _phase_snapshot(ProgressPhase.FAILED, now=_utc_now(), message=message),
            )
            with Session(engine) as session, session.begin():
                attempt = require_attempt(session, attempt_id)
                attempt.command_json = canonical_json(
                    {
                        "calibration": _calibration_command_payload(
                            result=calibration_result,
                            reused=None,
                        ),
                        "resource_snapshot": _resource_snapshot_payload(resource_snapshot),
                    }
                )
            job_transition_adapter.fail_job_after_external(
                engine,
                job_id=job_id,
                attempt_id=attempt_id,
                error=ExecutionError(message),
                now=_utc_now(),
            )
            return
        resource_decision = ResourceDecisionService().resolve(
            intent=intent,
            snapshot=resource_snapshot,
            evidence=calibration_result.selection,
        )
    execution_plan = _plan_with_resource_decision(plan, resource_decision)
    with Session(engine) as session, session.begin():
        attempt = require_attempt(session, attempt_id)
        attempt.command_json = canonical_json(
            {
                "effective_resume": effective_resume,
                "av1an_argv": build_av1an_command(
                    execution_plan.av1an,
                    resume=effective_resume,
                ),
                "resource_decision": _resource_decision_payload(resource_decision),
                "resource_snapshot": _resource_snapshot_payload(resource_snapshot),
                "calibration": _calibration_command_payload(
                    result=calibration_result,
                    reused=calibration_selection,
                ),
                "mux_spec": {
                    "video_input_path": str(plan.mux.video_input_path),
                    "source_input_path": str(plan.mux.source_input_path),
                    "output_path": str(plan.mux.output_path),
                },
            }
        )
        attempt.stdout_log = str(execution_plan.runtime.av1an_stdout_log)
        attempt.stderr_log = str(execution_plan.runtime.av1an_stderr_log)
        attempt.temp_dir = str(execution_plan.av1an.temp_dir)
        attempt.output_path = str(execution_plan.output_path)

    progress_bridge = CoalescingProgressBridge(
        lambda snapshot: _persist_attempt_progress(
            engine,
            attempt_id=attempt_id,
            snapshot=snapshot,
        )
    )
    persisted_progress_sink = ProgressPersistenceThrottle(progress_bridge)
    metrics_sink = _AttemptMetricsProgressSink()
    execution_progress_sink: ProgressSink = _ProgressFanoutSink(
        (metrics_sink, persisted_progress_sink, progress_sink)
        if progress_sink is not None
        else (metrics_sink, persisted_progress_sink)
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
    execution_thread = _ExecutionThread.start(
        execution_plan,
        cancellation_token=process_cancellation,
        progress_sink=execution_progress_sink,
    )
    try:
        status = _raise_execution_outcome(await _wait_execution_thread(execution_thread))
    except asyncio.CancelledError:
        process_cancellation.request("scheduler cancellation")
        outcome = await _wait_execution_thread(execution_thread)
        try:
            _raise_execution_outcome(outcome)
        except ExecutionInterruptedError:
            pass
        except ExecutionError:
            pass
        except asyncio.CancelledError:
            pass
        _save_attempt_progress(
            engine,
            attempt_id,
            _phase_snapshot(
                ProgressPhase.CANCELLED,
                now=_utc_now(),
                message="scheduler cancellation",
            ),
        )
        _persist_attempt_performance_observation_safely(
            engine=engine,
            job_id=job_id,
            attempt_id=attempt_id,
            plan=plan,
            resource_decision=resource_decision,
            environment_signature=environment_signature,
            workload_signature=workload_signature,
            metrics=metrics_sink.accumulator.finalize(status=AttemptStatus.CANCELED),
            now=_utc_now(),
        )
        with Session(engine) as session, session.begin():
            interrupt_job_stage(session, job_id=job_id, attempt_id=attempt_id, now=_utc_now())
        return
    except ExecutionInterruptedError:
        _save_attempt_progress(
            engine,
            attempt_id,
            _phase_snapshot(
                ProgressPhase.CANCELLED,
                now=_utc_now(),
                message="execution interrupted",
            ),
        )
        _persist_attempt_performance_observation_safely(
            engine=engine,
            job_id=job_id,
            attempt_id=attempt_id,
            plan=plan,
            resource_decision=resource_decision,
            environment_signature=environment_signature,
            workload_signature=workload_signature,
            metrics=metrics_sink.accumulator.finalize(status=AttemptStatus.INTERRUPTED),
            now=_utc_now(),
        )
        with Session(engine) as session, session.begin():
            interrupt_job_stage(session, job_id=job_id, attempt_id=attempt_id, now=_utc_now())
        return
    except ExecutionError as exc:
        _save_attempt_progress(
            engine,
            attempt_id,
            _phase_snapshot(
                ProgressPhase.FAILED,
                now=_utc_now(),
                message=str(exc),
            ),
        )
        _persist_attempt_performance_observation_safely(
            engine=engine,
            job_id=job_id,
            attempt_id=attempt_id,
            plan=plan,
            resource_decision=resource_decision,
            environment_signature=environment_signature,
            workload_signature=workload_signature,
            metrics=metrics_sink.accumulator.finalize(status=AttemptStatus.FAILED),
            now=_utc_now(),
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
    _persist_attempt_performance_observation_safely(
        engine=engine,
        job_id=job_id,
        attempt_id=attempt_id,
        plan=plan,
        resource_decision=resource_decision,
        environment_signature=environment_signature,
        workload_signature=workload_signature,
        metrics=metrics_sink.accumulator.finalize(status=AttemptStatus.COMPLETED),
        now=completed.observed_at,
    )
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


def _resolve_encode_resource_decision(
    plan: TranscodePlan,
    *,
    snapshot: ResourceSnapshot,
    capacity: ResourceCapacity,
    observations: tuple[CompatiblePerformanceObservation, ...] = (),
) -> ExecutionResourceDecision:
    intent = parse_resource_intent(
        workers=plan.av1an.workers,
        svt_lp=_svt_lp_from_encoder_args(plan.av1an.encoder_args),
    )
    selection = None
    try:
        candidates = generate_safe_concurrency_candidates(
            intent=intent,
            capacity=capacity,
            observations=observations,
        )
        estimates = estimate_candidates_from_history(observations, now=_utc_now())
        selection = select_estimated_candidate(
            candidates=candidates,
            estimates=estimates,
            capacity=capacity,
        )
    except Exception:
        selection = None
    return ResourceDecisionService().resolve(
        intent=intent,
        snapshot=snapshot,
        evidence=selection,
    )


def _selection_from_calibration(
    *,
    calibrations: tuple[CompatibleCalibrationObservation, ...],
    candidates: tuple[PerformanceCandidate, ...],
    calibration_key: str,
) -> MeasuredResourceSelection | None:
    candidate_by_key = {candidate_key(candidate): candidate for candidate in candidates}
    for calibration in calibrations:
        if calibration.calibration_key != calibration_key:
            continue
        if calibration.confidence < 0.6 or calibration.winner_json is None:
            continue
        try:
            payload = json.loads(calibration.winner_json)
            key = CandidateKey(
                workers=_candidate_workers(payload.get("workers")),
                svt_lp=_candidate_svt_lp(payload.get("svt_lp")),
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        candidate = candidate_by_key.get(key)
        if candidate is None:
            continue
        return MeasuredResourceSelection(
            candidate=candidate,
            confidence=calibration.confidence,
            reason="reused_calibration",
            evidence_count=1,
        )
    return None


def _candidate_workers(value: object) -> CandidateWorkerValue:
    if value == "auto":
        return "auto"
    if isinstance(value, int) and value > 0:
        return value
    raise ValueError("invalid calibrated worker count")


def _candidate_svt_lp(value: object) -> CandidateSvtLpValue:
    if value == "native":
        return "native"
    if isinstance(value, int) and value > 0:
        return value
    raise ValueError("invalid calibrated SVT lp")


def _source_frame_rate(
    session: Session,
    *,
    media_file: MediaFile,
    stream_index: int,
) -> float | None:
    probe = get_canonical_probe_result(session, media_file)
    if probe is None:
        return None
    try:
        normalized = parse_normalized_probe_json(probe.normalized_json)
        stream = next(item for item in normalized.video_streams if item.index == stream_index)
        if stream.fps is None:
            return None
        value = float(Fraction(stream.fps))
        return value if value > 0 else None
    except (LookupError, ValueError, ZeroDivisionError):
        return None


def _predicted_encode_seconds(
    *,
    plan: TranscodePlan,
    frame_rate: float | None,
    observations: tuple[CompatiblePerformanceObservation, ...],
) -> float:
    observed_fps = [item.aggregate_fps for item in observations if item.aggregate_fps > 0]
    if frame_rate is not None and observed_fps:
        frames = plan.validation.source_duration_seconds * frame_rate
        return frames / max(observed_fps)
    return plan.validation.source_duration_seconds


def _calibration_key(
    *,
    plan: TranscodePlan,
    environment_signature: ExecutionEnvironmentSignature,
    workload_signature: WorkloadSignature,
    candidates: tuple[PerformanceCandidate, ...],
    settings: PerformanceSettings,
) -> str:
    payload = {
        "schema_version": 2,
        "environment_signature_hash": environment_signature.signature_hash,
        "workload_signature_hash": workload_signature.signature_hash,
        "semantic_hash": plan.semantic_hash,
        "resource_policy_hash": plan.resource_policy_hash,
        "candidates": [_candidate_payload(candidate) for candidate in candidates],
        "sample_seconds": settings.calibration_sample_seconds,
        "warmup_seconds": settings.calibration_warmup_seconds,
        "minimum_gain_fraction": settings.calibration_min_gain_fraction,
    }
    encoded = f"avarch-calibration-v2\0{canonical_json(payload)}".encode()
    return hashlib.blake2b(encoded, digest_size=32).hexdigest()


def _candidate_payload(candidate: PerformanceCandidate) -> dict[str, object]:
    return {
        "workers": candidate.workers,
        "svt_lp": candidate.svt_lp,
        "reason": candidate.reason,
        "reservation": {
            "cpu": candidate.reservation.cpu,
            "memory_bytes": candidate.reservation.memory_bytes,
            "exclusive": candidate.reservation.exclusive,
        },
    }


def _measurement_payload(result: Any) -> dict[str, object]:
    metrics = result.metrics
    return {
        "workers": result.candidate_key.workers,
        "svt_lp": result.candidate_key.svt_lp,
        "status": result.status.value,
        "elapsed_seconds": result.elapsed_seconds,
        "aggregate_fps": metrics.aggregate_fps,
        "peak_rss_bytes": metrics.peak_rss_bytes,
        "average_cpu_utilization_percent": metrics.average_cpu_utilization_percent,
        "resource_attribution_available": metrics.resource_attribution_available,
        "failure_reason": result.failure_reason.value
        if result.failure_reason is not None
        else None,
        "command": list(result.command),
    }


def _calibration_command_payload(
    *,
    result: CalibrationExecutionResult | None,
    reused: MeasuredResourceSelection | None,
) -> dict[str, object]:
    if reused is not None:
        return {
            "status": "reused",
            "reason": reused.reason,
            "winner": {
                "workers": reused.effective_workers,
                "svt_lp": reused.effective_svt_lp,
            },
            "confidence": reused.confidence,
        }
    if result is None:
        return {"status": "not_required", "reason": "confident_history"}
    return {
        "status": result.status,
        "reason": result.reason,
        "budget_seconds": result.budget_seconds,
        "sample": _sample_payload(result),
        "measurements": [_measurement_payload(item) for item in result.measurements],
        "winner": (
            {
                "workers": result.selection.effective_workers,
                "svt_lp": result.selection.effective_svt_lp,
            }
            if result.selection is not None
            else None
        ),
        "confidence": result.selection.confidence if result.selection is not None else 0.0,
    }


def _calibration_incomplete_message(result: CalibrationExecutionResult) -> str:
    measured = len({item.candidate_key for item in result.measurements})
    announced = len(result.candidates)
    last_status = result.measurements[-1].status.value if result.measurements else "not_started"
    return (
        f"Calibration did not complete ({measured}/{announced} candidates; "
        f"last status: {last_status}; reason: {result.reason}). "
        "Production encode was not started. Increase "
        "performance.calibration_max_seconds or reduce calibration candidates/sample duration."
    )


def _sample_payload(result: CalibrationExecutionResult) -> dict[str, object] | None:
    sample = result.sample
    if sample is None:
        return None
    return {
        "start_seconds": sample.start_seconds,
        "duration_seconds": sample.duration_seconds,
        "estimated_frames": sample.estimated_frames,
        "parallel_units": sample.parallel_units,
    }


def _persist_calibration_result_safely(
    *,
    engine: Engine,
    plan: TranscodePlan,
    environment_signature: ExecutionEnvironmentSignature,
    workload_signature: WorkloadSignature,
    candidates: tuple[PerformanceCandidate, ...],
    settings: PerformanceSettings,
    result: CalibrationExecutionResult,
    now: datetime,
) -> None:
    if result.sample is None:
        return
    try:
        winner = (
            {
                "workers": result.selection.effective_workers,
                "svt_lp": result.selection.effective_svt_lp,
            }
            if result.selection is not None
            else None
        )
        with Session(engine) as session, session.begin():
            persist_calibration_observation(
                session,
                calibration_key=_calibration_key(
                    plan=plan,
                    environment_signature=environment_signature,
                    workload_signature=workload_signature,
                    candidates=candidates,
                    settings=settings,
                ),
                environment_signature=environment_signature,
                workload_signature=workload_signature,
                sample=_sample_payload(result) or {},
                candidates=tuple(_candidate_payload(item) for item in result.candidates),
                measurements=tuple(_measurement_payload(item) for item in result.measurements),
                winner=winner,
                status=result.status,
                confidence=result.selection.confidence if result.selection is not None else 0.0,
                measurement_cost_seconds=sum(
                    item.elapsed_seconds for item in result.measurements
                ),
                incomplete=result.incomplete,
                semantic_hash=plan.semantic_hash,
                resource_policy_hash=plan.resource_policy_hash,
                created_at=now,
            )
    except Exception:
        return


def _execute_plan_outcome(
    plan: TranscodePlan,
    *,
    cancellation_token: ProcessCancellationToken,
    progress_sink: ProgressSink,
) -> _ExecutionOutcome:
    try:
        return _ExecutionOutcome(
            status=execute_plan(
                plan,
                cancellation_token=cancellation_token,
                progress_sink=progress_sink,
            )
        )
    except ExecutionInterruptedError as exc:
        return _ExecutionOutcome(error_kind="interrupted", message=str(exc))
    except ResourceExhaustionError as exc:
        return _ExecutionOutcome(error_kind="resource_exhaustion", message=str(exc))
    except ExecutionError as exc:
        return _ExecutionOutcome(error_kind="execution_error", message=str(exc))
    except BaseException as exc:
        return _ExecutionOutcome(
            error_kind="unexpected_error",
            message=f"{type(exc).__name__}: {exc}",
        )


async def _wait_execution_thread(execution_thread: _ExecutionThread) -> _ExecutionOutcome:
    while not execution_thread.done:
        await asyncio.sleep(0.05)
    return execution_thread.outcome()


async def _wait_calibration_thread(
    calibration_thread: _CalibrationThread,
) -> CalibrationExecutionResult:
    while not calibration_thread.done:
        await asyncio.sleep(0.05)
    return calibration_thread.result()


def _raise_execution_outcome(outcome: _ExecutionOutcome) -> EncodeExecutionStatus:
    if outcome.status is not None:
        return outcome.status
    if outcome.error_kind == "interrupted":
        raise ExecutionInterruptedError(outcome.message)
    if outcome.error_kind == "resource_exhaustion":
        raise ResourceExhaustionError(outcome.message)
    if outcome.error_kind == "execution_error":
        raise ExecutionError(outcome.message)
    raise RuntimeError(outcome.message or "encode execution failed unexpectedly")


def _plan_with_resource_decision(
    plan: TranscodePlan,
    decision: ExecutionResourceDecision,
) -> TranscodePlan:
    return plan.model_copy(
        update={
            "av1an": plan.av1an.model_copy(
                update={
                    "workers": decision.effective_workers,
                    "encoder_args": _encoder_args_with_resource_decision(
                        plan.av1an.encoder_args,
                        decision,
                    ),
                }
            )
        }
    )


def _encoder_args_with_resource_decision(
    arguments: list[str],
    decision: ExecutionResourceDecision,
) -> list[str]:
    structured_lp = (
        decision.effective_svt_lp if isinstance(decision.effective_svt_lp, int) else "native"
    )
    return normalize_svt_operational_args(
        _strip_svt_lp(arguments),
        structured_svt_lp=structured_lp,
    )


def _strip_svt_lp(arguments: list[str]) -> list[str]:
    stripped: list[str] = []
    index = 0
    while index < len(arguments):
        token = arguments[index]
        option, separator, _inline_value = token.partition("=")
        if option not in LP_OPTIONS:
            stripped.append(token)
            index += 1
            continue
        index += 1 if separator else 2
    return stripped


def _selection_capacity(
    *,
    snapshot: ResourceSnapshot,
    reservation: JobResourceReservation | None,
    config: AppConfig,
) -> ResourceCapacity:
    limits = resolve_resource_limits(
        snapshot=snapshot,
        cpu_reserve=config.resources.cpu_reserve,
        memory_reserve=parse_memory_reserve(config.resources.memory_reserve),
    )
    return ResourceCapacity(
        cheap_workers=1,
        av1an_jobs=1,
        file_ops=1,
        cpu_budget=(
            reservation.cpu
            if reservation is not None and not reservation.exclusive
            else limits.usable_cpu_count
        ),
        memory_budget_bytes=(
            reservation.memory_bytes
            if reservation is not None and not reservation.exclusive
            else limits.usable_memory_bytes
        ),
    )


def _svt_lp_from_encoder_args(arguments: list[str]) -> int | None:
    definitions = svt_lp_definitions(arguments)
    if len(definitions) != 1:
        return None
    return definitions[0].value


def _resource_decision_payload(decision: ExecutionResourceDecision) -> dict[str, object]:
    return {
        "schema_version": 1,
        "mode": decision.mode.value,
        "effective_workers": decision.effective_workers,
        "effective_svt_lp": decision.effective_svt_lp,
        "reason": decision.reason,
        "confidence": decision.confidence,
        "algorithm_version": decision.algorithm_version,
        "fallback": decision.fallback,
        "evidence_count": decision.evidence_count,
    }


def _resource_snapshot_payload(snapshot: ResourceSnapshot) -> dict[str, object]:
    return {
        "effective_cpu_count": snapshot.effective_cpu_count,
        "effective_cpu_quota": snapshot.effective_cpu_quota,
        "effective_memory_bytes": snapshot.effective_memory_bytes,
        "cpu_sources": [
            {
                "source": value.source,
                "value": value.value,
                "confidence": value.confidence.value,
                "reason": value.reason,
            }
            for value in snapshot.cpu_values
        ],
        "memory_sources": [
            {
                "source": value.source,
                "value": value.value,
                "confidence": value.confidence.value,
                "reason": value.reason,
            }
            for value in snapshot.memory_values
        ],
    }


def _persist_attempt_performance_observation_safely(
    *,
    engine: Engine,
    job_id: int,
    attempt_id: int,
    plan: TranscodePlan,
    resource_decision: ExecutionResourceDecision,
    environment_signature: ExecutionEnvironmentSignature,
    workload_signature: WorkloadSignature,
    metrics: AttemptMetricsSummary,
    now: datetime,
) -> None:
    try:
        with Session(engine) as session, session.begin():
            job = require_job(session, job_id)
            attempt = require_attempt(session, attempt_id)
            persist_performance_observation(
                session,
                job=job,
                attempt=attempt,
                metrics=metrics,
                created_at=now,
                plan_hash=plan.plan_hash,
                semantic_hash=plan.semantic_hash,
                resource_policy_hash=plan.resource_policy_hash,
                resource_decision=_resource_decision_payload(resource_decision),
                tool_versions=_tool_versions_payload(plan),
                environment_signature=environment_signature,
                workload_signature=workload_signature,
            )
    except Exception:
        return


def _tool_versions_payload(plan: TranscodePlan) -> dict[str, object]:
    return {
        "av1an_version_family": plan.execution_identity.av1an_version_family,
        "vapoursynth_version": plan.vapoursynth.vapoursynth_version,
    }


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
    _save_attempt_progress(engine, attempt_id, snapshot)


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
