from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from sqlmodel import Session

from avarch.adapters.sqlite.models import MediaFile, MediaFileStatus, MediaPlan
from avarch.adapters.sqlite.probes import get_canonical_probe_result
from avarch.adapters.sqlite.queue import (
    create_queue_job,
    enqueue_candidate_media_files,
    find_existing_queue_job,
)
from avarch.adapters.sqlite.queue import (
    enqueue_plans as enqueue_sqlite_plans,
)
from avarch.adapters.sqlite.queue import (
    select_plans_for_enqueue as select_sqlite_plans_for_enqueue,
)
from avarch.application.enqueue import EnqueueCandidate, PlanEnqueueItem


class SqliteEnqueueStore:
    def __init__(self, session: Session) -> None:
        self._session = session

    def enqueue_candidates(
        self,
        *,
        media_file_ids: tuple[int, ...] | None,
    ) -> list[EnqueueCandidate]:
        candidates: list[EnqueueCandidate] = []
        for media_file in enqueue_candidate_media_files(
            self._session,
            media_file_ids=media_file_ids,
        ):
            canonical_probe = get_canonical_probe_result(self._session, media_file)
            candidates.append(
                EnqueueCandidate(
                    media_file_id=_require_id(media_file),
                    path=Path(media_file.path),
                    missing=_media_file_status_value(media_file.status)
                    == MediaFileStatus.MISSING.value,
                    fs_fingerprint=media_file.fs_fingerprint,
                    canonical_probe_id=canonical_probe.id if canonical_probe is not None else None,
                    canonical_probe_hash=(
                        canonical_probe.probe_hash if canonical_probe is not None else None
                    ),
                )
            )
        return candidates

    def queue_job_exists(self, *, queue_key: str) -> bool:
        return find_existing_queue_job(self._session, queue_key=queue_key) is not None

    def create_queue_job(
        self,
        *,
        candidate: EnqueueCandidate,
        profile_name: str,
        profile_hash: str,
        queue_key: str,
        priority: int,
        now: datetime,
    ) -> None:
        media_file = self._session.get(MediaFile, candidate.media_file_id)
        if media_file is None:
            raise ValueError(f"Media file no longer exists: {candidate.media_file_id}")
        create_queue_job(
            self._session,
            media_file=media_file,
            profile_name=profile_name,
            profile_hash=profile_hash,
            queue_key=queue_key,
            probe_result_id=candidate.canonical_probe_id,
            probe_hash=candidate.canonical_probe_hash,
            priority=priority,
            now=now,
        )

    def select_plans_for_enqueue(
        self,
        *,
        file_selectors: Sequence[Path] | None,
        plan_selectors: Sequence[str] | None,
        workspace_root: Path,
        resolve_path: Callable[[Path], Path],
    ) -> list[PlanEnqueueItem]:
        plans = select_sqlite_plans_for_enqueue(
            self._session,
            file_selectors=list(file_selectors) if file_selectors is not None else None,
            plan_selectors=list(plan_selectors) if plan_selectors is not None else None,
            workspace_root=workspace_root,
            resolve_path=resolve_path,
        )
        return [
            PlanEnqueueItem(id=_require_id(plan), plan_hash=plan.plan_hash)
            for plan in plans
        ]

    def enqueue_plans(
        self,
        *,
        plan_ids: tuple[int, ...],
        priority: int,
        now: datetime,
    ) -> dict[str, int]:
        selected_plans = [
            plan
            for plan_id in plan_ids
            if (plan := self._session.get(MediaPlan, plan_id)) is not None
        ]
        return enqueue_sqlite_plans(
            self._session,
            selected_plans,
            priority=priority,
            now=now,
        )


def _require_id(value: object) -> int:
    identifier = getattr(value, "id", None)
    if not isinstance(identifier, int):
        raise ValueError("Expected a persisted row id.")
    return identifier


def _media_file_status_value(status: MediaFileStatus | str) -> str:
    if isinstance(status, MediaFileStatus):
        return status.value
    return str(status)
