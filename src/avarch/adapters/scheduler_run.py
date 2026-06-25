from __future__ import annotations

from datetime import datetime

from sqlmodel import Session

from avarch.adapters.sqlite import job_transitions as job_transition_adapter
from avarch.adapters.sqlite import scheduler_state as scheduler_state_adapter
from avarch.adapters.sqlite.db import create_db_engine
from avarch.adapters.sqlite.job_transitions import interrupt_running_job
from avarch.adapters.sqlite.queue import claimable_jobs
from avarch.adapters.sqlite.scheduler_state import (
    active_jobs_with_cancel_requested,
    get_or_create_scheduler_state,
    has_pending_jobs,
    terminal_job_counts,
)
from avarch.application.scheduler_run import (
    SchedulerControlSnapshot,
    SchedulerRunStore,
    SchedulerTerminalCounts,
)
from avarch.config import AppConfig
from avarch.domain.jobs import JobStage
from avarch.domain.scheduler import ClaimableJob, SchedulerMode
from avarch.scheduler_support import encoded_output_exists, require_id
from avarch.scheduler_workers import (
    execute_encode_job,
    execute_plan_job,
    execute_probe_job,
    execute_promotion_job,
    execute_validation_job,
)


class SchedulerRuntimeAdapter:
    def store(self, *, config: AppConfig) -> SchedulerRunStore:
        return SqliteSchedulerRunStore(config)

    def workers(self) -> SchedulerWorkerAdapter:
        return SchedulerWorkerAdapter()


class SqliteSchedulerRunStore:
    def __init__(self, config: AppConfig) -> None:
        self._engine = create_db_engine(config.database.url)

    def acquire_lease(self, *, runner_id: str, now: datetime, resume: bool) -> None:
        with Session(self._engine) as session, session.begin():
            scheduler_state_adapter.acquire_scheduler_lease(
                session,
                runner_id=runner_id,
                now=now,
                resume=resume,
            )

    def recover_abandoned_jobs(self, *, now: datetime) -> None:
        with Session(self._engine) as session, session.begin():
            job_transition_adapter.recover_abandoned_jobs(
                session,
                now=now,
                encoded_output_exists=encoded_output_exists,
            )

    def renew_lease(self, *, runner_id: str, now: datetime) -> None:
        with Session(self._engine) as session, session.begin():
            scheduler_state_adapter.renew_scheduler_lease(
                session,
                runner_id=runner_id,
                now=now,
            )

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
        with Session(self._engine) as session, session.begin():
            scheduler_state_adapter.acknowledge_scheduler_control(
                session,
                runner_id=runner_id,
                now=now,
            )

    def cancel_requested_job_ids(self, *, job_ids: set[int]) -> set[int]:
        with Session(self._engine) as session:
            return active_jobs_with_cancel_requested(session, job_ids=job_ids)

    def claimable_jobs(self, *, active_job_ids: set[int]) -> list[ClaimableJob]:
        with Session(self._engine) as session:
            return [
                ClaimableJob(job_id=require_id(job), stage=job.stage)
                for job in claimable_jobs(session, active_job_ids=active_job_ids)
            ]

    def has_pending_jobs(self) -> bool:
        with Session(self._engine) as session:
            return has_pending_jobs(session)

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
    async def run_job(
        self,
        *,
        stage: JobStage,
        job_id: int,
        runner_id: str,
        config: AppConfig,
    ) -> None:
        worker = {
            JobStage.PROBE: execute_probe_job,
            JobStage.PLAN: execute_plan_job,
            JobStage.ENCODE: execute_encode_job,
            JobStage.VALIDATE: execute_validation_job,
            JobStage.PROMOTE: execute_promotion_job,
        }[stage]
        await worker(job_id=job_id, runner_id=runner_id, config=config)
