from __future__ import annotations

from avarch.adapters.scheduler_workers import execute_validation_job
from avarch.application.manual_validation import ManualValidationResult
from avarch.config import AppConfig


class SchedulerManualValidationWorker:
    async def validate_job(
        self,
        *,
        job_id: int,
        runner_id: str,
        config: AppConfig,
    ) -> ManualValidationResult | None:
        result = await execute_validation_job(job_id=job_id, runner_id=runner_id, config=config)
        if result is None:
            return None
        return ManualValidationResult(details_json=result.details_json)
