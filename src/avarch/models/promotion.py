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


class PromotionStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    FAILED = "failed"
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
    final_digest: str | None

    started_at: datetime
    updated_at: datetime
