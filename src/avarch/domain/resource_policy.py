from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

type NativeWorkerValue = Literal["auto"]
type SvtNativeValue = Literal["native"]


class ResourcePolicyError(ValueError):
    pass


class ResourceMode(StrEnum):
    AUTO = "auto"
    NATIVE = "native"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class WorkerIntent:
    mode: ResourceMode
    value: int | NativeWorkerValue

    @classmethod
    def native(cls) -> WorkerIntent:
        return cls(mode=ResourceMode.NATIVE, value="auto")

    @classmethod
    def manual(cls, value: int) -> WorkerIntent:
        if isinstance(value, bool) or value <= 0:
            raise ResourcePolicyError("manual workers must be a positive integer")
        return cls(mode=ResourceMode.MANUAL, value=value)


@dataclass(frozen=True, slots=True)
class SvtParallelismIntent:
    mode: ResourceMode
    value: int | SvtNativeValue

    @classmethod
    def native(cls) -> SvtParallelismIntent:
        return cls(mode=ResourceMode.NATIVE, value="native")

    @classmethod
    def manual(cls, value: int) -> SvtParallelismIntent:
        if isinstance(value, bool) or value <= 0:
            raise ResourcePolicyError("manual svt_lp must be a positive integer")
        return cls(mode=ResourceMode.MANUAL, value=value)


@dataclass(frozen=True, slots=True)
class ResourceIntent:
    mode: ResourceMode
    workers: WorkerIntent
    svt_lp: SvtParallelismIntent

    def __post_init__(self) -> None:
        if self.mode is ResourceMode.MANUAL and self.workers.mode is not ResourceMode.MANUAL:
            raise ResourcePolicyError("manual resource mode requires manual workers")
        if self.mode is ResourceMode.NATIVE:
            if self.workers.mode is not ResourceMode.NATIVE:
                raise ResourcePolicyError("native resource mode cannot use manual workers")
            if self.svt_lp.mode is not ResourceMode.NATIVE:
                raise ResourcePolicyError("native resource mode cannot use manual svt_lp")
        if self.mode is ResourceMode.AUTO and (
            self.workers.mode is ResourceMode.MANUAL or self.svt_lp.mode is ResourceMode.MANUAL
        ):
            raise ResourcePolicyError("auto resource mode cannot mix manual ownership")


def parse_resource_intent(
    *,
    mode: ResourceMode | str | None = None,
    workers: int | NativeWorkerValue = "auto",
    svt_lp: int | SvtNativeValue | None = None,
) -> ResourceIntent:
    resolved_mode = ResourceMode(mode) if mode is not None else _default_mode(workers)
    worker_intent = WorkerIntent.native() if workers == "auto" else WorkerIntent.manual(workers)
    svt_intent = (
        SvtParallelismIntent.native()
        if svt_lp is None or svt_lp == "native"
        else SvtParallelismIntent.manual(svt_lp)
    )
    if (
        resolved_mode is ResourceMode.AUTO
        and mode is None
        and worker_intent.mode is ResourceMode.MANUAL
    ):
        resolved_mode = ResourceMode.MANUAL
    if resolved_mode is ResourceMode.MANUAL and svt_intent.mode is ResourceMode.NATIVE:
        return ResourceIntent(mode=resolved_mode, workers=worker_intent, svt_lp=svt_intent)
    return ResourceIntent(mode=resolved_mode, workers=worker_intent, svt_lp=svt_intent)


def _default_mode(workers: int | NativeWorkerValue) -> ResourceMode:
    if workers == "auto":
        return ResourceMode.AUTO
    return ResourceMode.MANUAL
