from __future__ import annotations


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
