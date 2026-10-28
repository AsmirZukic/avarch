from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel


class PromotionMode(StrEnum):
    KEEP_ORIGINAL = "keep-original"
    MOVE_ORIGINAL_TO_BACKUP = "move-original-to-backup"
    REPLACE_ATOMIC = "replace-atomic"


class PromotionApproval(BaseModel):
    """Authority for one job and one immutable source/plan identity."""

    mode: PromotionMode
    plan_hash: str
    source_fingerprint: str
    approved_at: datetime
    approved_by: str
    delete_backup: bool = False


class PromotionStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
    RECOVERY_REQUIRED = "recovery_required"
    ROLLED_BACK = "rolled_back"


class PromotionPhase(StrEnum):
    PREPARED = "prepared"
    STAGING = "staging"
    STAGED = "staged"
    BACKUP_PENDING = "backup_pending"
    BACKUP_CREATED = "backup_created"
    INSTALL_PENDING = "install_pending"
    FINAL_INSTALLED = "final_installed"
    VERIFIED = "verified"
    COMMITTED = "committed"
    CLEANUP_COMPLETE = "cleanup_complete"
    RECOVERY_REQUIRED = "recovery_required"
    ROLLBACK_PENDING = "rollback_pending"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


class PromotionPolicy(BaseModel):
    schema_version: Literal[1] = 1

    allowed_modes: tuple[
        Literal[
            "keep-original",
            "move-original-to-backup",
            "replace-atomic",
        ],
        ...,
    ] = (
        PromotionMode.KEEP_ORIGINAL.value,
        PromotionMode.MOVE_ORIGINAL_TO_BACKUP.value,
        PromotionMode.REPLACE_ATOMIC.value,
    )

    keep_original_name_suffix: Literal[".av1"] = ".av1"
    backup_name_suffix: Literal[".avarch-original"] = ".avarch-original"

    require_validation: Literal[True] = True
    require_hardlink_for_replacement: Literal[True] = True
    cleanup_encode_temp_after_success: Literal[True] = True

    policy_hash: str


class FileStatSnapshot(BaseModel):
    size_bytes: int
    mtime_ns: int
    device_id: int
    inode: int
    mode: int


class PromotionJournal(BaseModel):
    schema_version: Literal[1] = 1

    promotion_id: int
    operation_id: str

    job_id: int
    validation_result_id: int

    mode: PromotionMode
    status: PromotionStatus
    phase: PromotionPhase

    source_path: Path
    validated_output_path: Path
    final_path: Path
    staging_path: Path
    backup_path: Path | None

    source_fingerprint_before: str
    source_stat: FileStatSnapshot

    validated_output_fingerprint: str
    validated_output_digest: str | None
    staging_digest: str | None
    staging_stat: FileStatSnapshot | None = None
    original_digest: str | None = None
    replacement_evidence_json: str | None = None
    delete_backup_approved: bool = False
    final_digest: str | None

    started_at: datetime
    updated_at: datetime
