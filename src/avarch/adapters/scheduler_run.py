from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime

from sqlmodel import Session

from avarch.adapters.job_preparation import encoded_output_exists, require_id
from avarch.adapters.scheduler_workers import (
    execute_cleanup_job,
    execute_encode_job,
    execute_plan_job,
    execute_probe_job,
    execute_promotion_job,
    execute_validation_job,
)
from avarch.adapters.sqlite import job_transitions as job_transition_adapter
from avarch.adapters.sqlite import scheduler_state as scheduler_state_adapter
from avarch.adapters.sqlite.db import create_db_engine
from avarch.adapters.sqlite.job_transitions import interrupt_running_job
from avarch.adapters.sqlite.queue import claimable_jobs
from avarch.adapters.sqlite.scheduler_state import (
    active_jobs_with_cancel_requested,
    get_or_create_scheduler_state,
    terminal_job_counts,
)
from avarch.application.promotion import PromotionWorkflow
from avarch.application.scheduler_run import (
    SchedulerAlreadyRunningError,
    SchedulerControlError,
    SchedulerControlSnapshot,
    SchedulerLeaseLostError,
    SchedulerRunStore,
    SchedulerTerminalCounts,
)
from avarch.config import AppConfig
from avarch.domain.jobs import JobStage
from avarch.domain.scheduler import ClaimableJob, SchedulerMode


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

    def acquire_lease(self, *, runner_id: str, now: datetime, resume: bool) -> None:
        try:
            with Session(self._engine) as session, session.begin():
                scheduler_state_adapter.acquire_scheduler_lease(
                    session,
                    runner_id=runner_id,
                    now=now,
                    resume=resume,
                )
        except scheduler_state_adapter.SchedulerAlreadyRunningError as exc:
            raise SchedulerAlreadyRunningError(str(exc)) from exc
        except scheduler_state_adapter.SchedulerControlError as exc:
            raise SchedulerControlError(str(exc)) from exc

    def recover_abandoned_jobs(self, *, now: datetime) -> None:
        with Session(self._engine) as session, session.begin():
            job_transition_adapter.recover_abandoned_jobs(
                session,
                now=now,
                encoded_output_exists=encoded_output_exists,
            )

    def renew_lease(self, *, runner_id: str, now: datetime) -> None:
        try:
            with Session(self._engine) as session, session.begin():
                scheduler_state_adapter.renew_scheduler_lease(
                    session,
                    runner_id=runner_id,
                    now=now,
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
            return [
                ClaimableJob(job_id=require_id(job), stage=job.stage)
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
            JobStage.ENCODE: execute_encode_job,
            JobStage.VALIDATE: execute_validation_job,
            JobStage.CLEANUP: execute_cleanup_job,
        }[stage]
        await worker(job_id=job_id, runner_id=runner_id, config=config)
