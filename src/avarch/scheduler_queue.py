from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlmodel import Session

from avarch.adapters.sqlite.models import MediaFileStatus
from avarch.adapters.sqlite.probes import get_canonical_probe_result
from avarch.adapters.sqlite.queue import (
    create_queue_job,
    enqueue_candidate_media_files,
    find_existing_queue_job,
)
from avarch.config import AppConfig
from avarch.scheduler_support import (
    build_queue_key,
    planning_identity,
    require_profile,
    status_value,
)


@dataclass(frozen=True, slots=True)
class EnqueueSummary:
    selected: int
    created: int
    existing: int
    missing_skipped: int


def enqueue_inventory(
    session: Session,
    *,
    config: AppConfig,
    profile_name: str,
    priority: int,
    now: datetime,
    media_file_ids: tuple[int, ...] | None = None,
) -> EnqueueSummary:
    resolved_profile = require_profile(config, profile_name)
    identity = planning_identity(resolved_profile)
    media_files = enqueue_candidate_media_files(session, media_file_ids=media_file_ids)
    selected = 0
    created = 0
    existing = 0
    missing_skipped = 0

    for media_file in media_files:
        if status_value(media_file.status) == MediaFileStatus.MISSING.value:
            missing_skipped += 1
            continue
        selected += 1
        canonical_probe = get_canonical_probe_result(session, media_file)
        probe_hash = canonical_probe.probe_hash if canonical_probe is not None else None
        queue_key = build_queue_key(
            media_path=Path(media_file.path),
            source_fs_fingerprint=media_file.fs_fingerprint,
            profile_name=resolved_profile.name,
            profile_hash=identity.profile_hash,
            probe_hash=probe_hash,
            vapoursynth_identity_hash=identity.vapoursynth_identity_hash,
            execution_identity_hash=identity.execution_identity_hash,
        )
        if find_existing_queue_job(session, queue_key=queue_key) is not None:
            existing += 1
            continue

        create_queue_job(
            session,
            media_file=media_file,
            profile_name=resolved_profile.name,
            profile_hash=identity.profile_hash,
            queue_key=queue_key,
            probe_result_id=canonical_probe.id if canonical_probe is not None else None,
            probe_hash=probe_hash,
            priority=priority,
            now=now,
        )
        created += 1

    return EnqueueSummary(
        selected=selected,
        created=created,
        existing=existing,
        missing_skipped=missing_skipped,
    )
