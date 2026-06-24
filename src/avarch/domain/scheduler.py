from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from avarch.domain.jobs import JobStage, ResourceClass


class SchedulerMode(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    DRAINING = "draining"
    STOPPING = "stopping"


@dataclass(frozen=True, slots=True)
class ResourceCapacity:
    cheap_workers: int
    av1an_jobs: int
    file_ops: int


def resource_for_stage(stage: JobStage) -> ResourceClass:
    if stage in {JobStage.PROBE, JobStage.PLAN, JobStage.VALIDATE}:
        return ResourceClass.CHEAP
    if stage == JobStage.ENCODE:
        return ResourceClass.HEAVY_AV1AN
    if stage == JobStage.PROMOTE:
        return ResourceClass.FILE_OP
    raise ValueError(f"Unsupported job stage: {stage}")


def has_resource_capacity(
    stage: JobStage,
    active_stages: Iterable[JobStage],
    *,
    capacity: ResourceCapacity,
) -> bool:
    resource_class = resource_for_stage(stage)
    active_count = sum(
        1 for active_stage in active_stages if resource_for_stage(active_stage) == resource_class
    )
    if resource_class == ResourceClass.CHEAP:
        return active_count < capacity.cheap_workers
    if resource_class == ResourceClass.HEAVY_AV1AN:
        return active_count < capacity.av1an_jobs
    if resource_class == ResourceClass.FILE_OP:
        return active_count < capacity.file_ops
    return False
