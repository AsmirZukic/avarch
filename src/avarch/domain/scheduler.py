from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from avarch.domain.jobs import (
    JobStage,
    JobStatus,
    ResourceClass,
    job_is_running,
    job_is_running_promotion,
    job_is_terminal_history,
)


class SchedulerMode(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    DRAINING = "draining"
    STOPPING = "stopping"


class QueueClearAction(StrEnum):
    CANCEL_IMMEDIATE = "cancel_immediate"
    REQUEST_RUNNING_CANCEL = "request_running_cancel"
    SKIP_RUNNING = "skip_running"
    EXCLUDE_RUNNING_PROMOTION = "exclude_running_promotion"
    EXCLUDE_TERMINAL = "exclude_terminal"


@dataclass(frozen=True, slots=True)
class ResourceCapacity:
    cheap_workers: int
    av1an_jobs: int
    file_ops: int
    nvenc_jobs: int = 1


@dataclass(frozen=True, slots=True)
class ActiveJob:
    job_id: int
    stage: JobStage
    resource_class: ResourceClass | None = None


@dataclass(frozen=True, slots=True)
class ClaimableJob:
    job_id: int
    stage: JobStage
    resource_class: ResourceClass | None = None


@dataclass(frozen=True, slots=True)
class RetryFacts:
    canonical_probe_matches: bool
    plan_artifact_valid: bool
    plan_identity_matches: bool
    output_exists: bool
    validation_passed: bool
    promotion_completed: bool


def resource_for_stage(
    stage: JobStage,
    *,
    backend: str = "av1an",
) -> ResourceClass:
    if stage in {JobStage.PROBE, JobStage.PLAN, JobStage.VALIDATE}:
        return ResourceClass.CHEAP
    if stage in {JobStage.SCENE_DETECT, JobStage.ENCODE}:
        return ResourceClass.HEAVY_NVENC if backend == "ffmpeg_nvenc" else ResourceClass.HEAVY_AV1AN
    if stage in {JobStage.PROMOTE, JobStage.CLEANUP}:
        return ResourceClass.FILE_OP
    raise ValueError(f"Unsupported job stage: {stage}")


def has_resource_capacity(
    stage: JobStage,
    active_stages: Iterable[JobStage],
    *,
    capacity: ResourceCapacity,
    backend: str = "av1an",
) -> bool:
    resource_class = resource_for_stage(stage, backend=backend)
    active_count = sum(
        1 for active_stage in active_stages if resource_for_stage(active_stage) == resource_class
    )
    if resource_class == ResourceClass.CHEAP:
        return active_count < capacity.cheap_workers
    if resource_class == ResourceClass.HEAVY_AV1AN:
        return active_count < capacity.av1an_jobs
    if resource_class == ResourceClass.HEAVY_NVENC:
        return active_count < capacity.nvenc_jobs
    if resource_class == ResourceClass.FILE_OP:
        return active_count < capacity.file_ops
    return False


def jobs_to_cancel(
    active_jobs: Iterable[ActiveJob],
    *,
    cancel_requested_job_ids: set[int],
    mode: SchedulerMode,
) -> frozenset[int]:
    if mode == SchedulerMode.STOPPING:
        return frozenset(job.job_id for job in active_jobs)
    return frozenset(job.job_id for job in active_jobs if job.job_id in cancel_requested_job_ids)


def scheduler_can_launch_jobs(mode: SchedulerMode) -> bool:
    return mode == SchedulerMode.RUNNING


def select_launchable_jobs(
    claimable_jobs: Iterable[ClaimableJob],
    *,
    active_jobs: Iterable[ActiveJob],
    capacity: ResourceCapacity,
) -> tuple[ClaimableJob, ...]:
    selected: list[ClaimableJob] = []
    planned_resources = [job.resource_class or resource_for_stage(job.stage) for job in active_jobs]
    limits = {
        ResourceClass.CHEAP: capacity.cheap_workers,
        ResourceClass.HEAVY_AV1AN: capacity.av1an_jobs,
        ResourceClass.HEAVY_NVENC: capacity.nvenc_jobs,
        ResourceClass.FILE_OP: capacity.file_ops,
    }
    for job in claimable_jobs:
        resource_class = job.resource_class or resource_for_stage(job.stage)
        if planned_resources.count(resource_class) >= limits[resource_class]:
            continue
        selected.append(job)
        planned_resources.append(resource_class)
    return tuple(selected)


def classify_queue_clear_job(
    status: JobStatus | str,
    stage: JobStage | str,
    *,
    cancel_running: bool,
) -> QueueClearAction:
    if job_is_terminal_history(status):
        return QueueClearAction.EXCLUDE_TERMINAL
    if job_is_running_promotion(status, stage):
        return QueueClearAction.EXCLUDE_RUNNING_PROMOTION
    if job_is_running(status):
        return (
            QueueClearAction.REQUEST_RUNNING_CANCEL
            if cancel_running
            else QueueClearAction.SKIP_RUNNING
        )
    return QueueClearAction.CANCEL_IMMEDIATE


def select_retry_stage(facts: RetryFacts) -> JobStage | None:
    if not facts.canonical_probe_matches:
        return JobStage.PROBE
    if not facts.plan_artifact_valid or not facts.plan_identity_matches:
        return JobStage.PLAN
    if facts.output_exists:
        if facts.validation_passed:
            if facts.promotion_completed:
                return None
            return JobStage.PROMOTE
        return JobStage.VALIDATE
    return JobStage.SCENE_DETECT
