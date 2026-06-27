from __future__ import annotations

from collections.abc import Sequence

from avarch.adapters import scheduler_lifecycle
from avarch.application.scheduler_process import (
    SchedulerLifecycleError,
    SchedulerProcessMetadata,
    SchedulerProcessStatus,
    SchedulerWorkspace,
)


class SchedulerProcessAdapter:
    def launch_detached(
        self,
        *,
        workspace: SchedulerWorkspace,
        argv: Sequence[str],
        timeout_seconds: float = 10.0,
    ) -> SchedulerProcessMetadata:
        try:
            metadata = scheduler_lifecycle.launch_detached(
                workspace=workspace,
                argv=argv,
                timeout_seconds=timeout_seconds,
            )
        except scheduler_lifecycle.SchedulerLifecycleError as exc:
            raise SchedulerLifecycleError(str(exc)) from exc
        return _metadata(metadata)

    def verified_status(self, *, workspace: SchedulerWorkspace) -> SchedulerProcessStatus:
        try:
            status = scheduler_lifecycle.verified_status(workspace)
        except scheduler_lifecycle.SchedulerLifecycleError as exc:
            raise SchedulerLifecycleError(str(exc)) from exc
        return SchedulerProcessStatus(
            state=status.state,
            metadata=_metadata(status.metadata) if status.metadata is not None else None,
            stale_removed=status.stale_removed,
        )

    def terminate_scheduler(
        self,
        *,
        workspace: SchedulerWorkspace,
        force: bool,
        timeout_seconds: float,
    ) -> bool:
        try:
            return scheduler_lifecycle.terminate_scheduler(
                workspace=workspace,
                force=force,
                timeout_seconds=timeout_seconds,
            )
        except scheduler_lifecycle.SchedulerLifecycleError as exc:
            raise SchedulerLifecycleError(str(exc)) from exc

    def workspace_lock(self, *, workspace: SchedulerWorkspace) -> SchedulerProcessLockAdapter:
        paths = scheduler_lifecycle.runtime_paths(workspace)
        return SchedulerProcessLockAdapter(
            scheduler_lifecycle.SchedulerWorkspaceLock(paths.lock_path)
        )

    def write_current_metadata(
        self,
        *,
        workspace: SchedulerWorkspace,
        mode: str,
    ) -> SchedulerProcessMetadata:
        paths = scheduler_lifecycle.runtime_paths(workspace)
        metadata = scheduler_lifecycle.current_process_metadata(workspace=workspace, mode=mode)
        try:
            scheduler_lifecycle.write_metadata(paths, metadata)
        except scheduler_lifecycle.SchedulerLifecycleError as exc:
            raise SchedulerLifecycleError(str(exc)) from exc
        return _metadata(metadata)

    def remove_metadata(self, *, workspace: SchedulerWorkspace) -> None:
        scheduler_lifecycle.remove_metadata(scheduler_lifecycle.runtime_paths(workspace))


class SchedulerProcessLockAdapter:
    def __init__(self, lock: scheduler_lifecycle.SchedulerWorkspaceLock) -> None:
        self._lock = lock

    def acquire(self) -> None:
        try:
            self._lock.acquire()
        except scheduler_lifecycle.SchedulerLifecycleError as exc:
            raise SchedulerLifecycleError(str(exc)) from exc

    def release(self) -> None:
        self._lock.release()


def _metadata(
    metadata: scheduler_lifecycle.SchedulerProcessMetadata,
) -> SchedulerProcessMetadata:
    return SchedulerProcessMetadata(
        pid=metadata.pid,
        process_start_time=metadata.process_start_time,
        boot_id=metadata.boot_id,
        workspace_id=metadata.workspace_id,
        started_at=metadata.started_at,
        mode=metadata.mode,
        state=metadata.state,
        log_path=metadata.log_path,
    )
