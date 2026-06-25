from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlmodel import Session

from avarch.adapters.filesystem.plans import PlanArtifactLoadError, load_plan_artifact
from avarch.adapters.sqlite.inventory import (
    MediaFileNotFoundError,
)
from avarch.adapters.sqlite.inventory import (
    require_media_file as require_inventory_media_file,
)
from avarch.adapters.sqlite.models import Job, MediaFile, MediaFileStatus
from avarch.config import AppConfig
from avarch.contracts import QUEUE_CONTRACT
from avarch.domain.jobs import JobStage, JobStatus
from avarch.models.plan import TranscodePlan
from avarch.planner import build_execution_identity, build_profile_hash
from avarch.probe import ProbeError
from avarch.profiles.registry import ProfileRegistry, ResolvedProfile, UnknownProfileError
from avarch.scanner import create_file_snapshot
from avarch.serialization import canonical_json
from avarch.vapoursynth import (
    GENERATOR_VERSION,
    build_vapoursynth_identity_hash,
    resolve_vapoursynth_filter,
    resolve_vapoursynth_template,
)


class SchedulerError(RuntimeError):
    pass


class StaleJobSourceError(SchedulerError):
    pass


class StaleJobProfileError(SchedulerError):
    pass


class JobPreparationError(SchedulerError):
    pass


@dataclass(frozen=True, slots=True)
class PlanningIdentity:
    profile_hash: str
    vapoursynth_identity_hash: str
    execution_identity_hash: str


def build_queue_key(
    *,
    media_path: Path,
    source_fs_fingerprint: str,
    profile_name: str,
    profile_hash: str,
    probe_hash: str | None,
    vapoursynth_identity_hash: str,
    execution_identity_hash: str,
) -> str:
    payload_json = canonical_json(
        {
            "media_path": str(media_path.resolve()),
            "source_fs_fingerprint": source_fs_fingerprint,
            "profile_name": profile_name,
            "profile_hash": profile_hash,
            "probe_hash": probe_hash,
            "vapoursynth_identity_hash": vapoursynth_identity_hash,
            "execution_identity_hash": execution_identity_hash,
            "queue_contract": QUEUE_CONTRACT,
        }
    )
    payload = f"{QUEUE_CONTRACT}\0".encode() + payload_json.encode("utf-8")
    return hashlib.blake2b(payload, digest_size=32).hexdigest()


def planning_identity(resolved_profile: ResolvedProfile) -> PlanningIdentity:
    profile = resolved_profile.profile
    resolved_template = resolve_vapoursynth_template(profile)
    resolved_filter = resolve_vapoursynth_filter(profile)
    mode = (
        "custom_filter"
        if resolved_filter is not None
        else "custom_template"
        if resolved_template is not None
        else "generated"
    )
    template_hash = resolved_template.template_hash if resolved_template is not None else None
    script_hash = resolved_filter.script_hash if resolved_filter is not None else None
    vapoursynth_identity_hash = build_vapoursynth_identity_hash(
        generator_version=GENERATOR_VERSION,
        mode=mode,
        output_format="YUV420P10",
        resize_filter="spline36",
        template_hash=template_hash,
        script_hash=script_hash,
        filter_entrypoint=resolved_filter.entrypoint if resolved_filter is not None else None,
        filter_api_version=resolved_filter.api_version if resolved_filter is not None else None,
    )
    execution_identity = build_execution_identity()
    return PlanningIdentity(
        profile_hash=build_profile_hash(
            profile,
            template_hash=template_hash,
            script_hash=script_hash,
        ),
        vapoursynth_identity_hash=vapoursynth_identity_hash,
        execution_identity_hash=execution_identity.identity_hash,
    )


def load_job_plan(job: Job) -> TranscodePlan:
    if job.plan_path is None or job.plan_hash is None:
        raise JobPreparationError("Queued encode job has no persisted plan.")
    path = Path(job.plan_path)
    try:
        plan = load_plan_artifact(path)
    except PlanArtifactLoadError as exc:
        raise JobPreparationError(f"Unable to load queued plan: {path}") from exc
    if plan.plan_hash != job.plan_hash:
        raise JobPreparationError("Queued plan artifact does not match stored plan hash.")
    return plan


def verify_media_snapshot(media_file: MediaFile, *, expected_fingerprint: str) -> None:
    if status_value(media_file.status) == MediaFileStatus.MISSING.value:
        raise StaleJobSourceError("Source file is marked missing.")
    try:
        snapshot = create_file_snapshot(Path(media_file.path))
    except OSError as exc:
        raise StaleJobSourceError(f"Unable to inspect source file: {media_file.path}") from exc
    if snapshot.fs_fingerprint != expected_fingerprint:
        raise StaleJobSourceError("Source fingerprint changed after enqueue.")


def verify_job_profile(config: AppConfig, job: Job) -> None:
    identity = planning_identity(require_profile(config, job.profile_name))
    if identity.profile_hash != job.profile_hash:
        raise StaleJobProfileError("Profile changed after enqueue; re-enqueue this work.")


def output_exists(job: Job) -> bool:
    return job.output_path is not None and Path(job.output_path).is_file()


def encoded_output_exists(job: Job) -> bool:
    return job.output_path is not None and Path(job.output_path).exists()


def scheduler_error(error: Exception) -> Exception:
    if isinstance(error, SchedulerError | ProbeError):
        return error
    return JobPreparationError(str(error))


def snapshot_job(job: Job) -> Job:
    return Job(
        id=job.id,
        media_file_id=job.media_file_id,
        profile_name=job.profile_name,
        profile_hash=job.profile_hash,
        source_fs_fingerprint=job.source_fs_fingerprint,
        queue_key=job.queue_key,
        probe_result_id=job.probe_result_id,
        probe_hash=job.probe_hash,
        plan_hash=job.plan_hash,
        plan_path=job.plan_path,
        output_path=job.output_path,
        latest_validation_id=job.latest_validation_id,
        latest_promotion_id=job.latest_promotion_id,
        status=JobStatus(job.status),
        stage=JobStage(job.stage),
        priority=job.priority,
        attempts=job.attempts,
        claimed_by=job.claimed_by,
        last_error_type=job.last_error_type,
        last_error_message=job.last_error_message,
        skip_reason=job.skip_reason,
        cancel_requested_at=job.cancel_requested_at,
        cancel_requested_by=job.cancel_requested_by,
        cancel_reason=job.cancel_reason,
        canceled_at=job.canceled_at,
        hold_requested_at=job.hold_requested_at,
        hold_requested_by=job.hold_requested_by,
        hold_reason=job.hold_reason,
        held_at=job.held_at,
        created_at=job.created_at,
        updated_at=job.updated_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


def require_profile(config: AppConfig, profile_name: str) -> ResolvedProfile:
    try:
        return ProfileRegistry.from_config(config).get(profile_name)
    except UnknownProfileError as exc:
        raise JobPreparationError(f"Unknown profile: {profile_name}") from exc
    except Exception as exc:
        raise JobPreparationError(str(exc)) from exc


def source_media_file(session: Session, media_file_id: int) -> MediaFile:
    try:
        return require_inventory_media_file(session, media_file_id=media_file_id)
    except MediaFileNotFoundError as exc:
        raise JobPreparationError(str(exc)) from exc


def require_id(value: Any) -> int:
    identifier = getattr(value, "id", None)
    if not isinstance(identifier, int):
        raise JobPreparationError("Expected a persisted row id.")
    return identifier


def status_value(status: MediaFileStatus | str) -> str:
    if isinstance(status, MediaFileStatus):
        return status.value
    return str(status)


def config_data_dir(config: AppConfig) -> Path:
    return Path(config.app.data_dir)
