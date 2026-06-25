from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from avarch.config import AppConfig
from avarch.scheduler_support import build_queue_key, planning_identity, require_profile


@dataclass(frozen=True, slots=True)
class EnqueueSummary:
    selected: int
    created: int
    existing: int
    missing_skipped: int


@dataclass(frozen=True, slots=True)
class EnqueueCandidate:
    media_file_id: int
    path: Path
    missing: bool
    fs_fingerprint: str
    canonical_probe_id: int | None
    canonical_probe_hash: str | None


class EnqueueStore(Protocol):
    def enqueue_candidates(
        self,
        *,
        media_file_ids: tuple[int, ...] | None,
    ) -> list[EnqueueCandidate]: ...

    def queue_job_exists(self, *, queue_key: str) -> bool: ...

    def create_queue_job(
        self,
        *,
        candidate: EnqueueCandidate,
        profile_name: str,
        profile_hash: str,
        queue_key: str,
        priority: int,
        now: datetime,
    ) -> None: ...


def enqueue_inventory(
    store: EnqueueStore,
    *,
    config: AppConfig,
    profile_name: str,
    priority: int,
    now: datetime,
    media_file_ids: tuple[int, ...] | None = None,
) -> EnqueueSummary:
    resolved_profile = require_profile(config, profile_name)
    identity = planning_identity(resolved_profile)
    candidates = store.enqueue_candidates(media_file_ids=media_file_ids)
    selected = 0
    created = 0
    existing = 0
    missing_skipped = 0

    for candidate in candidates:
        if candidate.missing:
            missing_skipped += 1
            continue
        selected += 1
        queue_key = build_queue_key(
            media_path=candidate.path,
            source_fs_fingerprint=candidate.fs_fingerprint,
            profile_name=resolved_profile.name,
            profile_hash=identity.profile_hash,
            probe_hash=candidate.canonical_probe_hash,
            vapoursynth_identity_hash=identity.vapoursynth_identity_hash,
            execution_identity_hash=identity.execution_identity_hash,
        )
        if store.queue_job_exists(queue_key=queue_key):
            existing += 1
            continue

        store.create_queue_job(
            candidate=candidate,
            profile_name=resolved_profile.name,
            profile_hash=identity.profile_hash,
            queue_key=queue_key,
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
