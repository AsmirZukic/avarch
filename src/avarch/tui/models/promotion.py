from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class PromotionPreview:
    job_id: int
    mode: str
    source_path: Path
    validated_output_path: Path
    final_path: Path
    staging_path: Path
    backup_path: Path | None
    operation_steps: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PromotionResult:
    job_id: int
    operation_id: str
    status: str
    final_path: Path
