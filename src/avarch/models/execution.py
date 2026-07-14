from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class ProcessTerminationReason(StrEnum):
    EXITED = "exited"
    CANCELLED = "cancelled"
    FORCED_KILL = "forced_kill"


@dataclass(frozen=True, slots=True)
class ProcessResult:
    command: tuple[str, ...]
    return_code: int
    started_at: datetime
    finished_at: datetime
    termination_reason: ProcessTerminationReason

    def __post_init__(self) -> None:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not be before started_at")

    @property
    def succeeded(self) -> bool:
        return self.return_code == 0 and self.termination_reason is ProcessTerminationReason.EXITED

    @classmethod
    def exited(
        cls,
        *,
        command: tuple[str, ...],
        return_code: int,
        started_at: datetime,
        finished_at: datetime,
    ) -> ProcessResult:
        return cls(
            command=command,
            return_code=return_code,
            started_at=started_at,
            finished_at=finished_at,
            termination_reason=ProcessTerminationReason.EXITED,
        )

    @classmethod
    def cancelled(
        cls,
        *,
        command: tuple[str, ...],
        return_code: int,
        started_at: datetime,
        finished_at: datetime,
    ) -> ProcessResult:
        return cls(
            command=command,
            return_code=return_code,
            started_at=started_at,
            finished_at=finished_at,
            termination_reason=ProcessTerminationReason.CANCELLED,
        )

    @classmethod
    def killed(
        cls,
        *,
        command: tuple[str, ...],
        return_code: int,
        started_at: datetime,
        finished_at: datetime,
    ) -> ProcessResult:
        return cls(
            command=command,
            return_code=return_code,
            started_at=started_at,
            finished_at=finished_at,
            termination_reason=ProcessTerminationReason.FORCED_KILL,
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
