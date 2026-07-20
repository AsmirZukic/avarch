from __future__ import annotations

import os
import socket
from collections.abc import Callable, Iterable
from datetime import datetime

from sqlmodel import Session

from avarch.adapters.execution import pause_managed_processes, resume_managed_processes
from avarch.adapters.job_preparation import encoded_output_exists, load_job_plan, require_id
from avarch.adapters.scheduler_workers import (
    execute_cleanup_job,
    execute_encode_job,
    execute_plan_job,
    execute_probe_job,
    execute_promotion_job,
    execute_validation_job,
)
from avarch.adapters.sqlite import job_transitions as job_transition_adapter
from avarch.adapters.sqlite import scheduler_sessions as scheduler_session_adapter
from avarch.adapters.sqlite import scheduler_state as scheduler_state_adapter
from avarch.adapters.sqlite.db import create_db_engine
from avarch.adapters.sqlite.job_transitions import interrupt_running_job
from avarch.adapters.sqlite.models import Job
from avarch.adapters.sqlite.performance import compatible_performance_observations
from avarch.adapters.sqlite.queue import claimable_jobs
from avarch.adapters.sqlite.scheduler_state import (
    active_jobs_with_cancel_requested,
    get_or_create_scheduler_state,
    terminal_job_counts,
)
from avarch.application.environment_signature import build_execution_environment_signature
from avarch.application.promotion import PromotionWorkflow
from avarch.application.resource_decision import ResourceDecisionService
from avarch.application.resource_demand import ResourceDemandEstimator
from avarch.application.resources import ResourceSnapshot, effective_resource_snapshot
from avarch.application.scheduler_run import (
    SchedulerAlreadyRunningError,
    SchedulerCapacityUsage,
    SchedulerControlError,
    SchedulerControlSnapshot,
    SchedulerLeaseLostError,
    SchedulerRunStore,
    SchedulerTerminalCounts,
)
from avarch.application.workload_signature import build_workload_signature
from avarch.config import AppConfig
from avarch.domain.encoder_args import svt_lp_definitions
from avarch.domain.jobs import JobStage, ResourceClass
from avarch.domain.resource_policy import parse_resource_intent
from avarch.domain.scheduler import (
    ClaimableJob,
    JobResourceReservation,
    SchedulerMode,
    resource_for_stage,
)


class SchedulerRuntimeAdapter:
    def __init__(
        self,
        *,
        workers: SchedulerWorkerAdapter,
        claimable_stages: Iterable[JobStage] | None = None,
    ) -> None:
        self._workers = workers
        self._claimable_stages = (
            frozenset(claimable_stages) if claimable_stages is not None else None
        )

    def store(self, *, config: AppConfig) -> SchedulerRunStore:
        return SqliteSchedulerRunStore(config, claimable_stages=self._claimable_stages)

    def workers(self) -> SchedulerWorkerAdapter:
        return self._workers


class SqliteSchedulerRunStore:
    def __init__(
        self,
        config: AppConfig,
        *,
        claimable_stages: frozenset[JobStage] | None = None,
    ) -> None:
        self._engine = create_db_engine(config.database.url)
        self._claimable_stages = claimable_stages
        self._workspace_id = config.database.url

    def acquire_lease(
        self,
        *,
        runner_id: str,
        now: datetime,
        resume: bool,
        capacity: SchedulerCapacityUsage,
    ) -> None:
        try:
            with Session(self._engine) as session, session.begin():
                scheduler_state_adapter.acquire_scheduler_lease(
                    session,
                    runner_id=runner_id,
                    now=now,
                    resume=resume,
                    capacity=_capacity_usage(capacity),
                )
        except scheduler_state_adapter.SchedulerAlreadyRunningError as exc:
            raise SchedulerAlreadyRunningError(str(exc)) from exc
        except scheduler_state_adapter.SchedulerControlError as exc:
            raise SchedulerControlError(str(exc)) from exc

    def start_session(self, *, runner_id: str, now: datetime) -> int:
        with Session(self._engine) as session, session.begin():
            scheduler_session = scheduler_session_adapter.start_scheduler_session(
                session,
                owner_id=runner_id,
                workspace_id=self._workspace_id,
                pid=os.getpid(),
                host=socket.gethostname(),
                now=now,
            )
            session_id = scheduler_session.id
            if session_id is None:
                raise RuntimeError("Scheduler session was not persisted.")
            return session_id

    def recover_abandoned_jobs(self, *, now: datetime) -> None:
        with Session(self._engine) as session, session.begin():
            job_transition_adapter.recover_abandoned_jobs(
                session,
                now=now,
                encoded_output_exists=encoded_output_exists,
            )

    def renew_lease(
        self,
        *,
        runner_id: str,
        now: datetime,
        capacity: SchedulerCapacityUsage,
    ) -> None:
        try:
            with Session(self._engine) as session, session.begin():
                scheduler_state_adapter.renew_scheduler_lease(
                    session,
                    runner_id=runner_id,
                    now=now,
                    capacity=_capacity_usage(capacity),
                )
        except scheduler_state_adapter.SchedulerLeaseLostError as exc:
            raise SchedulerLeaseLostError(str(exc)) from exc

    def load_control_snapshot(self, *, now: datetime) -> SchedulerControlSnapshot:
        with Session(self._engine) as session:
            state = get_or_create_scheduler_state(session, now=now)
            return SchedulerControlSnapshot(
                mode=SchedulerMode(state.mode),
                control_generation=state.control_generation,
                acknowledged_generation=state.acknowledged_generation,
                runner_id=state.runner_id,
            )

    def acknowledge_control(self, *, runner_id: str, now: datetime) -> None:
        try:
            with Session(self._engine) as session, session.begin():
                scheduler_state_adapter.acknowledge_scheduler_control(
                    session,
                    runner_id=runner_id,
                    now=now,
                )
        except scheduler_state_adapter.SchedulerLeaseLostError as exc:
            raise SchedulerLeaseLostError(str(exc)) from exc

    def cancel_requested_job_ids(self, *, job_ids: set[int]) -> set[int]:
        with Session(self._engine) as session:
            return active_jobs_with_cancel_requested(session, job_ids=job_ids)

    def claimable_jobs(self, *, active_job_ids: set[int]) -> list[ClaimableJob]:
        with Session(self._engine) as session:
            snapshot = effective_resource_snapshot()
            return [
                ClaimableJob(
                    job_id=require_id(job),
                    stage=job.stage,
                    reservation=_claimable_reservation(
                        session=session,
                        job=job,
                        snapshot=snapshot,
                    ),
                )
                for job in claimable_jobs(session, active_job_ids=active_job_ids)
                if self._claimable_stages is None or job.stage in self._claimable_stages
            ]

    def has_pending_jobs(self) -> bool:
        return bool(self.claimable_jobs(active_job_ids=set()))

    def interrupt_running_job(self, *, job_id: int, now: datetime) -> None:
        interrupt_running_job(self._engine, job_id=job_id, now=now)

    def release_lease(self, *, runner_id: str, now: datetime) -> None:
        with Session(self._engine) as session, session.begin():
            scheduler_state_adapter.release_scheduler_lease(
                session,
                runner_id=runner_id,
                now=now,
            )

    def end_session(self, *, session_id: int, now: datetime, reason: str) -> None:
        with Session(self._engine) as session, session.begin():
            scheduler_session_adapter.end_scheduler_session(
                session,
                session_id=session_id,
                now=now,
                reason=reason,
            )

    def terminal_counts(self) -> SchedulerTerminalCounts:
        with Session(self._engine) as session:
            counts = terminal_job_counts(session)
            return SchedulerTerminalCounts(
                completed=counts.completed,
                failed=counts.failed,
                skipped=counts.skipped,
            )


class SchedulerWorkerAdapter:
    def __init__(self, *, promotion_workflow_factory: Callable[[], PromotionWorkflow]) -> None:
        self._promotion_workflow_factory = promotion_workflow_factory

    async def run_job(
        self,
        *,
        stage: JobStage,
        job_id: int,
        runner_id: str,
        config: AppConfig,
        reservation: JobResourceReservation | None = None,
    ) -> None:
        if stage == JobStage.PROMOTE:
            await execute_promotion_job(
                job_id=job_id,
                runner_id=runner_id,
                config=config,
                promotion_workflow=self._promotion_workflow_factory(),
            )
            return
        worker = {
            JobStage.PROBE: execute_probe_job,
            JobStage.PLAN: execute_plan_job,
            JobStage.VALIDATE: execute_validation_job,
            JobStage.CLEANUP: execute_cleanup_job,
        }.get(stage)
        if worker is None:
            await execute_encode_job(
                job_id=job_id,
                runner_id=runner_id,
                config=config,
                reservation=reservation,
            )
            return
        await worker(job_id=job_id, runner_id=runner_id, config=config)

    def pause_active_jobs(self) -> None:
        pause_managed_processes()

    def resume_active_jobs(self) -> None:
        resume_managed_processes()


def _capacity_usage(
    capacity: SchedulerCapacityUsage,
) -> scheduler_state_adapter.SchedulerCapacityUsage:
    return scheduler_state_adapter.SchedulerCapacityUsage(
        cheap_workers=capacity.cheap_workers,
        cheap_active=capacity.cheap_active,
        av1an_jobs=capacity.av1an_jobs,
        av1an_active=capacity.av1an_active,
        file_ops=capacity.file_ops,
        file_ops_active=capacity.file_ops_active,
    )


def _claimable_reservation(
    *,
    session: Session,
    job: Job,
    snapshot: ResourceSnapshot,
) -> JobResourceReservation | None:
    stage = JobStage(job.stage)
    if resource_for_stage(stage) is ResourceClass.HEAVY_AV1AN:
        return _heavy_job_reservation(session=session, job=job, snapshot=snapshot)
    return None


def _heavy_job_reservation(
    *,
    session: Session,
    job: Job,
    snapshot: ResourceSnapshot,
) -> JobResourceReservation:
    try:
        plan = load_job_plan(job)
        decision = ResourceDecisionService().resolve(
            intent=parse_resource_intent(
                workers=plan.av1an.workers,
                svt_lp=_svt_lp_from_encoder_args(plan.av1an.encoder_args),
            ),
            snapshot=snapshot,
        )
        tool_versions = {
            "av1an_version_family": plan.execution_identity.av1an_version_family,
            "vapoursynth_version": plan.vapoursynth.vapoursynth_version,
        }
        environment_signature = build_execution_environment_signature(
            snapshot=snapshot,
            tool_versions=tool_versions,
        )
        workload_signature = build_workload_signature(plan)
        observations = compatible_performance_observations(
            session,
            environment_signature_hash=environment_signature.signature_hash,
            workload_signature_hash=workload_signature.signature_hash,
            semantic_hash=plan.semantic_hash,
            limit=10,
        )
        demand = ResourceDemandEstimator().estimate(
            decision=decision,
            observations=observations,
        )
    except Exception:
        return JobResourceReservation(exclusive=True)
    if demand.requires_exclusive_heavy_job:
        return JobResourceReservation(exclusive=True)
    if demand.cpu.maximum is None or demand.memory_bytes.maximum is None:
        return JobResourceReservation(exclusive=True)
    return JobResourceReservation(
        cpu=float(demand.cpu.maximum),
        memory_bytes=int(demand.memory_bytes.maximum),
        exclusive=False,
    )


def _svt_lp_from_encoder_args(arguments: list[str]) -> int | None:
    definitions = svt_lp_definitions(arguments)
    if len(definitions) != 1:
        return None
    return definitions[0].value
