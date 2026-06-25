from __future__ import annotations

from pathlib import Path

from sqlmodel import Session

from avarch.adapters.filesystem.plans import PlanArtifactLoadError, load_plan_artifact
from avarch.adapters.promotion_service import (
    PromotionError,
    execute_promotion,
    promote_job,
    recover_promotion,
    validate_promotion_preflight,
)
from avarch.adapters.sqlite.db import create_db_engine
from avarch.adapters.sqlite.job_transitions import JobClaimError, require_job
from avarch.adapters.sqlite.models import PromotionRecord
from avarch.adapters.sqlite.promotions import has_completed_promotion
from avarch.adapters.sqlite.validations import latest_validation
from avarch.application.promotion import (
    PromotionPreflightView,
    PromotionRecordView,
    PromotionResultView,
    PromotionWorkflowError,
)
from avarch.config import AppConfig
from avarch.models.promotion import PromotionMode


class PromotionWorkflowAdapter:
    def preflight(
        self,
        *,
        job_id: int,
        mode: PromotionMode,
        config: AppConfig,
        operation_id: str,
    ) -> PromotionPreflightView:
        engine = create_db_engine(config.database.url)
        try:
            with Session(engine) as session:
                job = require_job(session, job_id)
                if job.plan_path is None:
                    raise PromotionWorkflowError("Job has no plan artifact.")
                try:
                    plan = load_plan_artifact(Path(job.plan_path))
                except PlanArtifactLoadError as exc:
                    raise PromotionWorkflowError(
                        "Job plan artifact is not usable. Regenerate the plan for this job."
                    ) from exc
                validation = latest_validation(session, job)
                if validation is None:
                    raise PromotionWorkflowError("Job has no current validation result.")
                if has_completed_promotion(session, job):
                    raise PromotionWorkflowError("Job already has a completed promotion.")
                preflight = validate_promotion_preflight(
                    job=job,
                    plan=plan,
                    validation=validation,
                    mode=mode,
                    operation_id=operation_id,
                )
        except JobClaimError as exc:
            raise PromotionWorkflowError(f"Job not found: {job_id}") from exc
        except PromotionError as exc:
            raise PromotionWorkflowError(str(exc)) from exc
        return PromotionPreflightView(
            job_id=preflight.job_id,
            validation_result_id=preflight.validation_result_id,
            mode=preflight.mode,
            source_path=preflight.source_path,
            validated_output_path=preflight.validated_output_path,
            final_path=preflight.final_path,
            staging_path=preflight.staging_path,
            backup_path=preflight.backup_path,
            warnings=tuple(preflight.warnings),
        )

    async def execute(
        self,
        *,
        job_id: int,
        mode: PromotionMode,
        config: AppConfig,
        owner_token: str,
    ) -> PromotionRecordView:
        try:
            record = await execute_promotion(
                job_id=job_id,
                mode=mode,
                config=config,
                owner_token=owner_token,
            )
        except PromotionError as exc:
            raise PromotionWorkflowError(str(exc)) from exc
        return _record_view(record)

    async def recover(
        self,
        *,
        job_id: int,
        config: AppConfig,
        owner_token: str,
    ) -> PromotionRecordView:
        try:
            record = await recover_promotion(
                job_id=job_id,
                config=config,
                owner_token=owner_token,
            )
        except PromotionError as exc:
            raise PromotionWorkflowError(str(exc)) from exc
        return _record_view(record)

    async def promote(
        self,
        *,
        job_id: int,
        config: AppConfig,
        mode: PromotionMode = PromotionMode.REPLACE_ATOMIC,
        owner_token: str | None = None,
    ) -> PromotionResultView:
        result = await promote_job(
            job_id,
            config=config,
            mode=mode,
            owner_token=owner_token,
        )
        return PromotionResultView(
            job_id=result.job_id,
            promotion_id=result.promotion_id,
            status=result.status,
            final_path=result.final_path,
            promoted=result.promoted,
            error_message=result.error_message,
        )


def _record_view(record: PromotionRecord) -> PromotionRecordView:
    return PromotionRecordView(
        id=record.id,
        job_id=record.job_id,
        mode=record.mode,
        source_path=record.source_path,
        backup_path=record.backup_path,
        final_path=record.final_path,
        validation_result_id=record.validation_result_id,
        cleanup_completed=record.cleanup_completed,
        cleanup_error=record.cleanup_error,
    )
