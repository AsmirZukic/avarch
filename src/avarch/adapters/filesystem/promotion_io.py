"""Filesystem-only promotion operations; no database or recovery policy."""

from __future__ import annotations

import errno
import os
import shutil
from pathlib import Path
from typing import BinaryIO, Protocol

from avarch.adapters.filesystem.promotion import (
    PROMOTION_DIGEST_CHUNK_SIZE,
    calculate_promotion_digest,
    create_promotion_digest,
    fsync_directory,
)
from avarch.adapters.filesystem.scanner import create_file_snapshot
from avarch.adapters.promotion_types import (
    PromotionConflictError,
    PromotionEligibilityError,
    PromotionFilesystemError,
    PromotionVerificationError,
    StagedOutput,
)
from avarch.models.promotion import FileStatSnapshot


class CopyHeartbeat(Protocol):
    def refresh(self) -> None: ...


class StagingFileFactory(Protocol):
    def __call__(self, path: Path, *, mode: int) -> BinaryIO: ...


def create_promotion_staging_file(
    path: Path,
    *,
    mode: int,
) -> BinaryIO:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags, mode & 0o777)
    except FileExistsError as exc:
        raise PromotionConflictError(f"Staging path already exists: {path}") from exc
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PromotionConflictError(f"Staging path is a symlink: {path}") from exc
        raise PromotionFilesystemError(f"Unable to create staging file: {path}") from exc
    return os.fdopen(fd, "wb")


def stage_validated_output_sync(
    *,
    source_output: Path,
    staging_path: Path,
    expected_fingerprint: str,
    expected_mode: int,
    heartbeat: CopyHeartbeat,
    create_staging_file: StagingFileFactory,
) -> StagedOutput:
    before = create_file_snapshot(source_output)
    if before.fs_fingerprint != expected_fingerprint:
        raise PromotionEligibilityError("Validated output fingerprint changed before staging.")

    digest = create_promotion_digest()
    bytes_copied = 0
    owned_staging: FileStatSnapshot | None = None
    try:
        with (
            source_output.open("rb") as input_file,
            create_staging_file(
                staging_path,
                mode=expected_mode,
            ) as output_file,
        ):
            owned_staging = snapshot_from_stat(os.fstat(output_file.fileno()))
            while chunk := input_file.read(PROMOTION_DIGEST_CHUNK_SIZE):
                output_file.write(chunk)
                digest.update(chunk)
                bytes_copied += len(chunk)
                heartbeat.refresh()
            output_file.flush()
            os.fsync(output_file.fileno())
        fsync_directory(staging_path.parent)
    except Exception:
        if owned_staging is not None:
            unlink_owned_staging(staging_path, owned_staging)
        raise

    after = create_file_snapshot(source_output)
    if before.fs_fingerprint != after.fs_fingerprint:
        unlink_owned_staging(staging_path, owned_staging)
        raise PromotionVerificationError("Validated output changed during staging.")

    source_digest = digest.hexdigest()
    staging_digest = calculate_promotion_digest(staging_path, heartbeat=heartbeat.refresh)
    if source_digest != staging_digest:
        unlink_owned_staging(staging_path, owned_staging)
        raise PromotionVerificationError("Staging digest does not match validated output.")
    return StagedOutput(
        source_digest=source_digest,
        staging_digest=staging_digest,
        size_bytes=bytes_copied,
    )


def create_original_rollback_link(
    *,
    source_path: Path,
    backup_path: Path,
    expected_source: FileStatSnapshot,
) -> None:
    reject_symlink(source_path, "Source path")
    if backup_path.exists() or backup_path.is_symlink():
        raise PromotionConflictError(f"Backup path already exists: {backup_path}")
    source_stat = stat_snapshot(source_path)
    if source_stat != expected_source:
        raise PromotionVerificationError("Source changed before rollback backup creation.")
    try:
        os.link(source_path, backup_path, follow_symlinks=False)
    except OSError as exc:
        raise PromotionFilesystemError("Unable to create hard-link rollback backup.") from exc
    fsync_directory(source_path.parent)
    verify_hardlink_backup(
        source_path=source_path,
        backup_path=backup_path,
        expected_source=expected_source,
    )


def same_inode(path: Path, expected: FileStatSnapshot) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    actual = stat_snapshot(path)
    return (actual.device_id, actual.inode) == (expected.device_id, expected.inode)


def delete_known_work_path(path: Path, *, work_dir: Path, recursive: bool = False) -> None:
    if not path.exists() and not path.is_symlink():
        return
    resolved_work = work_dir.resolve()
    try:
        path.resolve().relative_to(resolved_work)
    except ValueError as exc:
        raise PromotionFilesystemError(f"Cleanup path is outside work directory: {path}") from exc
    if path.is_symlink():
        raise PromotionFilesystemError(f"Cleanup path is a symlink: {path}")
    if path.is_dir():
        if not recursive:
            raise PromotionFilesystemError(f"Cleanup path is an unexpected directory: {path}")
        shutil.rmtree(path)
        fsync_directory(path.parent)
        return
    path.unlink()
    fsync_directory(path.parent)


def require_regular_nonsymlink(path: Path, label: str) -> None:
    reject_symlink(path, label)
    if not path.exists():
        raise PromotionEligibilityError(f"{label} does not exist: {path}")
    if not path.is_file():
        raise PromotionEligibilityError(f"{label} is not a regular file: {path}")


def reject_symlink(path: Path, label: str) -> None:
    if path.is_symlink():
        raise PromotionEligibilityError(f"{label} is a symlink: {path}")


def stat_snapshot(path: Path) -> FileStatSnapshot:
    return snapshot_from_stat(path.stat(follow_symlinks=False))


def snapshot_from_stat(stat_result: os.stat_result) -> FileStatSnapshot:
    return FileStatSnapshot(
        size_bytes=stat_result.st_size,
        mtime_ns=stat_result.st_mtime_ns,
        device_id=stat_result.st_dev,
        inode=stat_result.st_ino,
        mode=stat_result.st_mode,
    )


def verify_hardlink_backup(
    *,
    source_path: Path,
    backup_path: Path,
    expected_source: FileStatSnapshot,
    same_inode: bool = True,
) -> None:
    source_stat = stat_snapshot(source_path)
    backup_stat = stat_snapshot(backup_path)
    if same_inode and (
        source_stat.device_id != backup_stat.device_id or source_stat.inode != backup_stat.inode
    ):
        raise PromotionVerificationError("Rollback backup is not a hard link to the source.")
    if (
        backup_stat.device_id != expected_source.device_id
        or backup_stat.inode != expected_source.inode
        or backup_stat.size_bytes != expected_source.size_bytes
        or backup_stat.mtime_ns != expected_source.mtime_ns
    ):
        raise PromotionVerificationError("Rollback backup does not match the original source.")


def unlink_owned_staging(staging_path: Path, expected: FileStatSnapshot | None) -> None:
    if not staging_path.exists() and not staging_path.is_symlink():
        return
    if expected is None or not same_inode(staging_path, expected):
        raise PromotionConflictError("Staging ownership changed; refusing cleanup.")
    staging_path.unlink()
    fsync_directory(staging_path.parent)
