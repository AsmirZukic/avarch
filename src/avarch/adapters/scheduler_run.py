from __future__ import annotations

from avarch.application.scheduler_run import SchedulerRunSummary
from avarch.config import AppConfig
from avarch.scheduler_runner import run_scheduler


class SchedulerRunnerAdapter:
    async def run_scheduler(
        self,
        *,
        config: AppConfig,
        runner_id: str,
        resume: bool,
    ) -> SchedulerRunSummary:
        summary = await run_scheduler(config=config, runner_id=runner_id, resume=resume)
        return SchedulerRunSummary(
            completed=summary.completed,
            failed=summary.failed,
            skipped=summary.skipped,
            idle=summary.idle,
        )
