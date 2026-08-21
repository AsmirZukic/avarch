from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path

from sqlmodel import Session

from avarch.adapters.sqlite.models import MediaPlan
from avarch.adapters.sqlite.queue import (
    enqueue_plans as enqueue_sqlite_plans,
)
from avarch.adapters.sqlite.queue import (
    select_plans_for_enqueue as select_sqlite_plans_for_enqueue,
)
from avarch.application.enqueue import PlanEnqueueItem


class SqliteEnqueueStore:
    def __init__(self, session: Session) -> None:
        self._session = session

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
        return [PlanEnqueueItem(id=_require_id(plan), plan_hash=plan.plan_hash) for plan in plans]

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
