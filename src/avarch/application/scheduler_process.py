from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from avarch.workspace import WorkspaceContext


class SchedulerLifecycleError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SchedulerProcessMetadata:
    pid: int
    process_start_time: int
    boot_id: str
    workspace_id: str
    started_at: str
    mode: str
    state: str
    log_path: str


@dataclass(frozen=True, slots=True)
class SchedulerProcessStatus:
    state: str
    metadata: SchedulerProcessMetadata | None
    stale_removed: bool = False


class SchedulerProcessLock(Protocol):
    def acquire(self) -> None: ...

    def release(self) -> None: ...


class SchedulerProcessController(Protocol):
    def launch_detached(
        self,
        *,
        workspace: WorkspaceContext,
        argv: Sequence[str],
        timeout_seconds: float = 10.0,
    ) -> SchedulerProcessMetadata: ...

    def verified_status(self, *, workspace: WorkspaceContext) -> SchedulerProcessStatus: ...

    def terminate_scheduler(
        self,
        *,
        workspace: WorkspaceContext,
        force: bool,
        timeout_seconds: float,
    ) -> bool: ...

    def workspace_lock(self, *, workspace: WorkspaceContext) -> SchedulerProcessLock: ...

    def write_current_metadata(
        self,
        *,
        workspace: WorkspaceContext,
        mode: str,
    ) -> SchedulerProcessMetadata: ...

    def remove_metadata(self, *, workspace: WorkspaceContext) -> None: ...


def launch_detached(
    controller: SchedulerProcessController,
    *,
    workspace: WorkspaceContext,
    argv: Sequence[str],
    timeout_seconds: float = 10.0,
) -> SchedulerProcessMetadata:
    return controller.launch_detached(
        workspace=workspace,
        argv=argv,
        timeout_seconds=timeout_seconds,
    )


def verified_status(
    controller: SchedulerProcessController,
    *,
    workspace: WorkspaceContext,
) -> SchedulerProcessStatus:
    return controller.verified_status(workspace=workspace)


def terminate_scheduler(
    controller: SchedulerProcessController,
    *,
    workspace: WorkspaceContext,
    force: bool,
    timeout_seconds: float,
) -> bool:
    return controller.terminate_scheduler(
        workspace=workspace,
        force=force,
        timeout_seconds=timeout_seconds,
    )


def workspace_lock(
    controller: SchedulerProcessController,
    *,
    workspace: WorkspaceContext,
) -> SchedulerProcessLock:
    return controller.workspace_lock(workspace=workspace)


def write_current_metadata(
    controller: SchedulerProcessController,
    *,
    workspace: WorkspaceContext,
    mode: str,
) -> SchedulerProcessMetadata:
    return controller.write_current_metadata(workspace=workspace, mode=mode)


def remove_metadata(
    controller: SchedulerProcessController,
    *,
    workspace: WorkspaceContext,
) -> None:
    controller.remove_metadata(workspace=workspace)
