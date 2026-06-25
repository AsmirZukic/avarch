from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from avarch.config import AppConfig
from avarch.models.promotion import PromotionMode, PromotionStatus


class PromotionWorkflowError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PromotionPreflightView:
    job_id: int
    validation_result_id: int
    mode: PromotionMode
    source_path: Path
    validated_output_path: Path
    final_path: Path
    staging_path: Path
    backup_path: Path | None
    warnings: Sequence[str]


@dataclass(frozen=True, slots=True)
class PromotionRecordView:
    id: int | None
    job_id: int
    mode: PromotionMode
    source_path: str
    backup_path: str | None
    final_path: str
    validation_result_id: int
    cleanup_completed: bool
    cleanup_error: str | None


@dataclass(frozen=True, slots=True)
class PromotionResultView:
    job_id: int
    promotion_id: int
    status: PromotionStatus
    final_path: Path
    promoted: bool
    error_message: str | None = None


class PromotionWorkflow(Protocol):
    def preflight(
        self,
        *,
        job_id: int,
        mode: PromotionMode,
        config: AppConfig,
        operation_id: str,
    ) -> PromotionPreflightView: ...

    async def execute(
        self,
        *,
        job_id: int,
        mode: PromotionMode,
        config: AppConfig,
        owner_token: str,
    ) -> PromotionRecordView: ...

    async def recover(
        self,
        *,
        job_id: int,
        config: AppConfig,
        owner_token: str,
    ) -> PromotionRecordView: ...

    async def promote(
        self,
        *,
        job_id: int,
        config: AppConfig,
        mode: PromotionMode = PromotionMode.REPLACE_ATOMIC,
        owner_token: str | None = None,
    ) -> PromotionResultView: ...


def promotion_preflight(
    workflow: PromotionWorkflow,
    *,
    job_id: int,
    mode: PromotionMode,
    config: AppConfig,
    operation_id: str,
) -> PromotionPreflightView:
    return workflow.preflight(
        job_id=job_id,
        mode=mode,
        config=config,
        operation_id=operation_id,
    )


async def execute_promotion(
    workflow: PromotionWorkflow,
    *,
    job_id: int,
    mode: PromotionMode,
    config: AppConfig,
    owner_token: str,
) -> PromotionRecordView:
    return await workflow.execute(
        job_id=job_id,
        mode=mode,
        config=config,
        owner_token=owner_token,
    )


async def recover_promotion(
    workflow: PromotionWorkflow,
    *,
    job_id: int,
    config: AppConfig,
    owner_token: str,
) -> PromotionRecordView:
    return await workflow.recover(job_id=job_id, config=config, owner_token=owner_token)


async def promote_job(
    workflow: PromotionWorkflow,
    *,
    job_id: int,
    config: AppConfig,
    mode: PromotionMode = PromotionMode.REPLACE_ATOMIC,
    owner_token: str | None = None,
) -> PromotionResultView:
    return await workflow.promote(
        job_id=job_id,
        config=config,
        mode=mode,
        owner_token=owner_token,
    )
