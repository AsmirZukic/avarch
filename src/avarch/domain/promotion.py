from __future__ import annotations

from pathlib import Path


class PromotionPathConflictError(ValueError):
    pass


def derive_keep_original_path(source_path: Path, *, container: str) -> Path:
    final_path = source_path.with_name(f"{source_path.stem}.av1.{container}")
    if final_path == source_path:
        raise PromotionPathConflictError("Derived promotion path would overwrite the source path.")
    return final_path


def derive_backup_path(source_path: Path) -> Path:
    return source_path.with_name(source_path.name + ".avarch-original")


def derive_staging_path(final_path: Path, *, operation_id: str) -> Path:
    token = operation_id[:12]
    return final_path.with_name(f".{final_path.name}.avarch-promote-{token}.tmp")


def derive_promotion_journal_path(runtime_dir: Path) -> Path:
    return runtime_dir / "promotion-journal.json"