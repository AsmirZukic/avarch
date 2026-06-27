from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from avarch.config import AppConfig
from avarch.domain.jobs import ManualValidationAction


@dataclass(frozen=True, slots=True)
class ManualValidationResult:
    details_json: str


@dataclass(frozen=True, slots=True)
class ManualValidationPreparationView:
    action: ManualValidationAction
    job_id: int
    plan_path: str | None
    existing_validation_details_json: str | None = None


class ManualValidationPreparationStore(Protocol):
    def prepare_validation_for_job(
        self,
        *,
        job_id: int,
        now: datetime,
    ) -> ManualValidationPreparationView | None: ...

    def prepare_validation_for_output(
        self,
        *,
        media_file_id: int,
        output_path: str,
        now: datetime,
    ) -> ManualValidationPreparationView | None: ...


class ManualValidationWorker(Protocol):
    async def validate_job(
        self,
        *,
        job_id: int,
        runner_id: str,
        config: AppConfig,
    ) -> ManualValidationResult | None: ...


def prepare_validation_for_output(
    store: ManualValidationPreparationStore,
    *,
    media_file_id: int,
    output_path: str,
    now: datetime,
) -> ManualValidationPreparationView | None:
    return store.prepare_validation_for_output(
        media_file_id=media_file_id,
        output_path=output_path,
        now=now,
    )


def prepare_validation_for_job(
    store: ManualValidationPreparationStore,
    *,
    job_id: int,
    now: datetime,
) -> ManualValidationPreparationView | None:
    return store.prepare_validation_for_job(job_id=job_id, now=now)


async def run_validation_job(
    worker: ManualValidationWorker,
    *,
    job_id: int,
    runner_id: str,
    config: AppConfig,
) -> ManualValidationResult | None:
    return await worker.validate_job(job_id=job_id, runner_id=runner_id, config=config)
