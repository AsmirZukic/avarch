from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from avarch.models.promotion import FileStatSnapshot, PromotionMode, PromotionStatus


class PromotionError(RuntimeError):
    pass


class PromotionEligibilityError(PromotionError):
    pass


class PromotionConflictError(PromotionError):
    pass


class PromotionLeaseError(PromotionError):
    pass


class PromotionFilesystemError(PromotionError):
    pass


class PromotionVerificationError(PromotionError):
    pass


class PromotionRecoveryError(PromotionError):
    pass


class PromotionRollbackError(PromotionError):
    pass


class PromotionPersistenceError(PromotionError):
    pass


class PromotionPreflightResult(BaseModel):
    job_id: int
    validation_result_id: int

    mode: PromotionMode

    source_path: Path
    validated_output_path: Path

    final_path: Path
    staging_path: Path
    backup_path: Path | None

    source_fingerprint: str
    source_stat: FileStatSnapshot

    validated_output_fingerprint: str

    warnings: list[str]


@dataclass(frozen=True, slots=True)
class PromotionResult:
    job_id: int
    promotion_id: int
    status: PromotionStatus
    final_path: Path
    promoted: bool
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class StagedOutput:
    source_digest: str
    staging_digest: str
    size_bytes: int
