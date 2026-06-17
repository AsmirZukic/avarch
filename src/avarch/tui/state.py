from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from avarch.tui.models.queue import QueueFilters
from avarch.tui.models.workflow import WorkflowDraft


class TuiRoute(StrEnum):
    DASHBOARD = "dashboard"
    WORKFLOW = "workflow"
    LIBRARY = "library"
    QUEUE = "queue"
    PROFILES = "profiles"
    DIAGNOSTICS = "diagnostics"

    @property
    def label(self) -> str:
        return {
            TuiRoute.DASHBOARD: "Dashboard",
            TuiRoute.WORKFLOW: "New Workflow",
            TuiRoute.LIBRARY: "Library",
            TuiRoute.QUEUE: "Queue",
            TuiRoute.PROFILES: "Profiles",
            TuiRoute.DIAGNOSTICS: "Diagnostics",
        }[self]


PRIMARY_ROUTES: tuple[TuiRoute, ...] = (
    TuiRoute.DASHBOARD,
    TuiRoute.WORKFLOW,
    TuiRoute.LIBRARY,
    TuiRoute.QUEUE,
    TuiRoute.PROFILES,
    TuiRoute.DIAGNOSTICS,
)


@dataclass(slots=True)
class TuiSessionState:
    active_route: TuiRoute = TuiRoute.DASHBOARD
    workflow_draft: WorkflowDraft | None = None
    queue_filters: QueueFilters = field(default_factory=QueueFilters)
    focused_job_id: int | None = None
    focused_profile_name: str | None = None
    tui_owned_scheduler: bool = False
