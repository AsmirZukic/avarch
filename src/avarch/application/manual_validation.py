from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from avarch.config import AppConfig


@dataclass(frozen=True, slots=True)
class ManualValidationResult:
    details_json: str


class ManualValidationWorker(Protocol):
    async def validate_job(
        self,
        *,
        job_id: int,
        runner_id: str,
        config: AppConfig,
    ) -> ManualValidationResult | None: ...


async def run_validation_job(
    worker: ManualValidationWorker,
    *,
    job_id: int,
    runner_id: str,
    config: AppConfig,
) -> ManualValidationResult | None:
    return await worker.validate_job(job_id=job_id, runner_id=runner_id, config=config)
