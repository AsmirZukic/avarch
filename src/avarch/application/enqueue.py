from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class PlanEnqueueItem:
    id: int
    plan_hash: str


@dataclass(frozen=True, slots=True)
class PlanEnqueueSummary:
    selected: int
    created: int
    skipped: int
    already_queued: int
    already_done: int
    stale: int
    plan_hashes: tuple[str, ...]


class EnqueueStore(Protocol):
    def select_plans_for_enqueue(
        self,
        *,
        file_selectors: Sequence[Path] | None,
        plan_selectors: Sequence[str] | None,
        workspace_root: Path,
        resolve_path: Callable[[Path], Path],
    ) -> list[PlanEnqueueItem]: ...

    def enqueue_plans(
        self,
        *,
        plan_ids: tuple[int, ...],
        priority: int,
        now: datetime,
    ) -> dict[str, int]: ...


def enqueue_selected_plans(
    store: EnqueueStore,
    *,
    file_selectors: Sequence[Path] | None,
    plan_selectors: Sequence[str] | None,
    workspace_root: Path,
    resolve_path: Callable[[Path], Path],
    priority: int,
    now: datetime,
) -> PlanEnqueueSummary:
    selected_plans = store.select_plans_for_enqueue(
        file_selectors=file_selectors,
        plan_selectors=plan_selectors,
        workspace_root=workspace_root,
        resolve_path=resolve_path,
    )
    summary = store.enqueue_plans(
        plan_ids=tuple(plan.id for plan in selected_plans),
        priority=priority,
        now=now,
    )
    return PlanEnqueueSummary(
        selected=len(selected_plans),
        created=summary["created"],
        skipped=summary["skipped"],
        already_queued=summary["already_queued"],
        already_done=summary["already_done"],
        stale=summary["stale"],
        plan_hashes=tuple(plan.plan_hash for plan in selected_plans),
    )
