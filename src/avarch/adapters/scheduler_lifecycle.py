from __future__ import annotations

import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from avarch.workspace import WorkspaceContext


class SchedulerLifecycleError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SchedulerRuntimePaths:
    run_dir: Path
    lock_path: Path
    metadata_path: Path
    log_path: Path


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
class SchedulerRuntimeStatus:
    state: str
    metadata: SchedulerProcessMetadata | None
    stale_removed: bool = False


class SchedulerWorkspaceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise SchedulerLifecycleError(
                "Another scheduler process holds the workspace lock."
            ) from exc
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        fcntl.flock(self._fd, fcntl.LOCK_UN)
        os.close(self._fd)
        self._fd = None

    def __enter__(self) -> Self:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self.release()


def runtime_paths(workspace: WorkspaceContext) -> SchedulerRuntimePaths:
    run_dir = workspace.root / ".avarch" / "run"
    logs_dir = workspace.root / ".avarch" / "logs"
    return SchedulerRuntimePaths(
        run_dir=run_dir,
        lock_path=run_dir / "scheduler.lock",
        metadata_path=run_dir / "scheduler.json",
        log_path=logs_dir / "scheduler.log",
    )


def workspace_id(workspace: WorkspaceContext) -> str:
    return str(workspace.root.resolve())


def process_start_time(pid: int) -> int:
    stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    return int(stat.rsplit(")", maxsplit=1)[1].split()[19])


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def current_process_metadata(
    *,
    workspace: WorkspaceContext,
    mode: str,
    state: str = "running",
) -> SchedulerProcessMetadata:
    paths = runtime_paths(workspace)
    return SchedulerProcessMetadata(
        pid=os.getpid(),
        process_start_time=process_start_time(os.getpid()),
        boot_id=boot_id(),
        workspace_id=workspace_id(workspace),
        started_at=datetime.now(UTC).isoformat(),
        mode=mode,
        state=state,
        log_path=str(paths.log_path),
    )


def write_metadata(paths: SchedulerRuntimePaths, metadata: SchedulerProcessMetadata) -> None:
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    paths.log_path.parent.mkdir(parents=True, exist_ok=True)
    paths.metadata_path.write_text(
        json.dumps(_metadata_to_json(metadata), indent=2),
        encoding="utf-8",
    )


def remove_metadata(paths: SchedulerRuntimePaths) -> None:
    paths.metadata_path.unlink(missing_ok=True)


def read_metadata(paths: SchedulerRuntimePaths) -> SchedulerProcessMetadata | None:
    try:
        data = json.loads(paths.metadata_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError) as exc:
        raise SchedulerLifecycleError(
            f"Scheduler metadata is unreadable: {paths.metadata_path}"
        ) from exc
    return SchedulerProcessMetadata(
        pid=int(data["pid"]),
        process_start_time=int(data["process_start_time"]),
        boot_id=str(data["boot_id"]),
        workspace_id=str(data["workspace_id"]),
        started_at=str(data["started_at"]),
        mode=str(data["mode"]),
        state=str(data["state"]),
        log_path=str(data["log_path"]),
    )


def verified_status(workspace: WorkspaceContext) -> SchedulerRuntimeStatus:
    paths = runtime_paths(workspace)
    metadata = read_metadata(paths)
    if metadata is None:
        return SchedulerRuntimeStatus(state="stopped", metadata=None)
    if not process_matches(metadata, workspace=workspace):
        remove_metadata(paths)
        return SchedulerRuntimeStatus(state="stopped", metadata=None, stale_removed=True)
    return SchedulerRuntimeStatus(state="running", metadata=metadata)


def process_matches(metadata: SchedulerProcessMetadata, *, workspace: WorkspaceContext) -> bool:
    if metadata.workspace_id != workspace_id(workspace):
        return False
    if metadata.boot_id != boot_id():
        return False
    try:
        if process_start_time(metadata.pid) != metadata.process_start_time:
            return False
    except OSError:
        return False
    return True


def launch_detached(
    *,
    workspace: WorkspaceContext,
    argv: Sequence[str],
    timeout_seconds: float = 10.0,
) -> SchedulerProcessMetadata:
    paths = runtime_paths(workspace)
    existing = verified_status(workspace)
    if existing.metadata is not None:
        raise SchedulerLifecycleError("Scheduler is already running.")
    paths.log_path.parent.mkdir(parents=True, exist_ok=True)
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, "-m", "avarch", *argv, "--managed-child", "--mode", "detached"]
    with paths.log_path.open("ab", buffering=0) as log_file:
        child = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
            close_fds=True,
            cwd=workspace.root,
        )

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if child.poll() is not None:
            tail = tail_log(paths.log_path)
            raise SchedulerLifecycleError(
                f"Scheduler startup failed with exit code {child.returncode}.\n{tail}"
            )
        metadata = read_metadata(paths)
        if metadata is not None and process_matches(metadata, workspace=workspace):
            return metadata
        time.sleep(0.1)

    child.terminate()
    tail = tail_log(paths.log_path)
    raise SchedulerLifecycleError(f"Timed out waiting for scheduler startup.\n{tail}")


def terminate_scheduler(
    *,
    workspace: WorkspaceContext,
    force: bool,
    timeout_seconds: float,
) -> bool:
    status = verified_status(workspace)
    metadata = status.metadata
    if metadata is None:
        return False
    signum = signal.SIGKILL if force else signal.SIGTERM
    try:
        os.killpg(metadata.pid, signum)
    except ProcessLookupError:
        try:
            os.kill(metadata.pid, signum)
        except ProcessLookupError:
            remove_metadata(runtime_paths(workspace))
            return False
    except PermissionError as exc:
        raise SchedulerLifecycleError(
            f"Unable to signal scheduler process {metadata.pid}."
        ) from exc

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if verified_status(workspace).metadata is None:
            return True
        time.sleep(0.1)
    if force:
        return False
    return terminate_scheduler(workspace=workspace, force=True, timeout_seconds=timeout_seconds)


def tail_log(path: Path, *, limit: int = 8192) -> str:
    if not path.exists():
        return ""
    data = path.read_bytes()
    if len(data) > limit:
        data = data[-limit:]
    return data.decode("utf-8", errors="replace").rstrip()


def _metadata_to_json(metadata: SchedulerProcessMetadata) -> dict[str, Any]:
    return {
        "pid": metadata.pid,
        "process_start_time": metadata.process_start_time,
        "boot_id": metadata.boot_id,
        "workspace_id": metadata.workspace_id,
        "started_at": metadata.started_at,
        "mode": metadata.mode,
        "state": metadata.state,
        "log_path": metadata.log_path,
    }
