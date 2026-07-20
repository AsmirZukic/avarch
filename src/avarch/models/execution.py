from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from threading import Event, Lock


class ProcessCancellationToken:
    def __init__(self) -> None:
        self._event = Event()
        self._lock = Lock()
        self._reason: str | None = None

    @property
    def cancel_requested(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    def request(self, reason: str | None = None) -> bool:
        with self._lock:
            if self._event.is_set():
                return False
            self._reason = reason
            self._event.set()
            return True

    def wait(self, *, timeout_seconds: float | None = None) -> bool:
        return self._event.wait(timeout_seconds)


class ProcessTerminationReason(StrEnum):
    EXITED = "exited"
    CANCELLED = "cancelled"
    FORCED_KILL = "forced_kill"


class ProcessFailureReason(StrEnum):
    CANCELLED = "cancelled"
    RESOURCE_OOM = "resource_oom"
    RESOURCE_PRESSURE = "resource_pressure"
    PROCESS_SIGNAL = "process_signal"
    ENCODER_FAILURE = "encoder_failure"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ProcessResourceSummary:
    peak_rss_bytes: int | None = None
    peak_swap_bytes: int | None = None
    memory_oom_events_delta: int | None = None
    memory_oom_kill_events_delta: int | None = None
    swap_current_bytes_delta: int | None = None
    cpu_throttled_events_delta: int | None = None
    cpu_throttled_usec_delta: int | None = None
    attribution_available: bool = False


@dataclass(frozen=True, slots=True)
class ProcessResult:
    command: tuple[str, ...]
    return_code: int
    started_at: datetime
    finished_at: datetime
    termination_reason: ProcessTerminationReason
    termination_requested_at: datetime | None = None
    resource_summary: ProcessResourceSummary | None = None

    def __post_init__(self) -> None:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not be before started_at")
        if self.termination_requested_at is None:
            return
        if self.termination_requested_at < self.started_at:
            raise ValueError("termination_requested_at must not be before started_at")
        if self.finished_at < self.termination_requested_at:
            raise ValueError("finished_at must not be before termination_requested_at")

    @property
    def succeeded(self) -> bool:
        return self.return_code == 0 and self.termination_reason is ProcessTerminationReason.EXITED

    @property
    def failure_reason(self) -> ProcessFailureReason | None:
        if self.succeeded:
            return None
        if self.termination_reason in {
            ProcessTerminationReason.CANCELLED,
            ProcessTerminationReason.FORCED_KILL,
        }:
            return ProcessFailureReason.CANCELLED
        summary = self.resource_summary
        if summary is not None:
            if (summary.memory_oom_kill_events_delta or 0) > 0:
                return ProcessFailureReason.RESOURCE_OOM
            if (
                (summary.memory_oom_events_delta or 0) > 0
                or (summary.swap_current_bytes_delta or 0) > 0
                or (summary.cpu_throttled_events_delta or 0) > 0
            ):
                return ProcessFailureReason.RESOURCE_PRESSURE
        if self.return_code < 0:
            return ProcessFailureReason.PROCESS_SIGNAL
        if self.return_code > 0:
            return ProcessFailureReason.ENCODER_FAILURE
        return ProcessFailureReason.UNKNOWN

    @classmethod
    def exited(
        cls,
        *,
        command: tuple[str, ...],
        return_code: int,
        started_at: datetime,
        finished_at: datetime,
        resource_summary: ProcessResourceSummary | None = None,
    ) -> ProcessResult:
        return cls(
            command=command,
            return_code=return_code,
            started_at=started_at,
            finished_at=finished_at,
            termination_reason=ProcessTerminationReason.EXITED,
            resource_summary=resource_summary,
        )

    @classmethod
    def cancelled(
        cls,
        *,
        command: tuple[str, ...],
        return_code: int,
        started_at: datetime,
        finished_at: datetime,
        termination_requested_at: datetime | None = None,
        resource_summary: ProcessResourceSummary | None = None,
    ) -> ProcessResult:
        return cls(
            command=command,
            return_code=return_code,
            started_at=started_at,
            finished_at=finished_at,
            termination_reason=ProcessTerminationReason.CANCELLED,
            termination_requested_at=termination_requested_at,
            resource_summary=resource_summary,
        )

    @classmethod
    def killed(
        cls,
        *,
        command: tuple[str, ...],
        return_code: int,
        started_at: datetime,
        finished_at: datetime,
        termination_requested_at: datetime | None = None,
        resource_summary: ProcessResourceSummary | None = None,
    ) -> ProcessResult:
        return cls(
            command=command,
            return_code=return_code,
            started_at=started_at,
            finished_at=finished_at,
            termination_reason=ProcessTerminationReason.FORCED_KILL,
            termination_requested_at=termination_requested_at,
            resource_summary=resource_summary,
        )


class ExecutionError(RuntimeError):
    pass


class PreflightError(ExecutionError):
    pass


class ToolUnavailableError(PreflightError):
    pass


class UnsupportedToolVersionError(PreflightError):
    pass


class InvalidExecutionPlanError(PreflightError):
    pass


class StaleExecutionPlanError(PreflightError):
    pass


class WorkDirectoryConflictError(ExecutionError):
    pass


class Av1anStageError(ExecutionError):
    pass


class MuxStageError(ExecutionError):
    pass


class ExecutionInterruptedError(ExecutionError):
    pass


class ResourceExhaustionError(ExecutionError):
    def __init__(
        self,
        message: str,
        *,
        resource_summary: ProcessResourceSummary | None = None,
    ) -> None:
        super().__init__(message)
        self.resource_summary = resource_summary
        self.failure_reason = ProcessFailureReason.RESOURCE_OOM
