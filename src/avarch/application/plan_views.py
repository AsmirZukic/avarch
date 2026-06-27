from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class PlanListItem:
    id: int | None
    media_file_id: int
    media_path: str | None
    profile_name: str
    plan_hash: str
    current: bool


@dataclass(frozen=True, slots=True)
class PlanDetail:
    id: int | None
    media_path: str | None
    profile_name: str
    current: bool
    plan_hash: str
    probe_hash: str
    output_path: str
    plan_path: str


class PlanViewStore(Protocol):
    def list_plans(self, *, current_only: bool) -> list[PlanListItem]: ...

    def plan_detail(self, *, selector: str) -> PlanDetail | None: ...


def list_plans(
    store: PlanViewStore,
    *,
    current_only: bool,
) -> list[PlanListItem]:
    return store.list_plans(current_only=current_only)


def plan_detail(store: PlanViewStore, *, selector: str) -> PlanDetail | None:
    return store.plan_detail(selector=selector)
