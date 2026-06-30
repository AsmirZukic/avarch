from __future__ import annotations

from datetime import datetime
from pathlib import Path

from sqlmodel import Session, col, select

from avarch.adapters.sqlite.inventory import PathResolver, select_inventory_files
from avarch.adapters.sqlite.models import Job, MediaFile, MediaFileStatus, MediaPlan
from avarch.adapters.sqlite.planning import current_plan_for_file, find_plan
from avarch.domain.jobs import JobStage, JobStatus, job_has_passed_validation


class QueueSelectionError(ValueError):
    pass


def find_existing_queue_job(
    session: Session,
    *,
    queue_key: str,
) -> Job | None:
    return session.exec(select(Job).where(Job.queue_key == queue_key)).first()


def plan_hash_conflicts_with_other_job(
    session: Session,
    *,
    plan_hash: str,
    job_id: int,
) -> bool:
    return (
        session.exec(select(Job).where(Job.plan_hash == plan_hash, Job.id != job_id)).first()
        is not None
    )


def queue_key_conflicts_with_other_job(
    session: Session,
    *,
    queue_key: str,
    job_id: int | None,
) -> bool:
    return (
        session.exec(select(Job).where(Job.queue_key == queue_key, Job.id != job_id)).first()
        is not None
    )


def enqueue_candidate_media_files(
    session: Session,
    *,
    media_file_ids: tuple[int, ...] | None,
) -> list[MediaFile]:
    statement = select(MediaFile).order_by(MediaFile.path)
    if media_file_ids is not None:
        statement = statement.where(col(MediaFile.id).in_(media_file_ids))
    return list(session.exec(statement).all())


def create_queue_job(
    session: Session,
    *,
    media_file: MediaFile,
    profile_name: str,
    profile_hash: str,
    queue_key: str,
    probe_result_id: int | None,
    probe_hash: str | None,
    priority: int,
    now: datetime,
) -> None:
    session.add(
        Job(
            media_file_id=_require_id(media_file),
            profile_name=profile_name,
            profile_hash=profile_hash,
            source_fs_fingerprint=media_file.fs_fingerprint,
            queue_key=queue_key,
            probe_result_id=probe_result_id,
            probe_hash=probe_hash,
            status=JobStatus.QUEUED,
            stage=JobStage.PLAN if probe_result_id is not None else JobStage.PROBE,
            priority=priority,
            attempts=0,
            created_at=now,
            updated_at=now,
        )
    )


def claimable_jobs(session: Session, *, active_job_ids: set[int]) -> list[Job]:
    jobs = list(
        session.exec(
            select(Job)
            .where(
                col(Job.status).in_(
                    [
                        JobStatus.QUEUED,
                        JobStatus.ENCODED,
                        JobStatus.VALIDATING,
                        JobStatus.READY_TO_PROMOTE,
                        JobStatus.PROMOTING,
                    ]
                )
            )
            .order_by(col(Job.priority).desc(), col(Job.created_at).asc(), col(Job.id).asc())
        ).all()
    )
    return [
        job
        for job in jobs
        if job.id not in active_job_ids
        and (
            job.status == JobStatus.QUEUED
            or (
                job.stage == JobStage.VALIDATE
                and job.status in {JobStatus.ENCODED, JobStatus.VALIDATING}
            )
            or (
                job.stage == JobStage.PROMOTE
                and (
                    job_has_passed_validation(job.status, job.stage)
                    or job.status == JobStatus.PROMOTING
                )
            )
        )
    ]


def select_queue_jobs(
    session: Session,
    *,
    job_ids: set[int] | None,
    statuses: set[JobStatus] | None,
    stages: set[JobStage] | None,
    profile: str | None,
    all_jobs: bool,
) -> list[Job]:
    if not all_jobs and not job_ids and not statuses and not stages and profile is None:
        raise QueueSelectionError("At least one selector is required.")
    statement = select(Job).order_by(
        col(Job.priority).desc(),
        col(Job.created_at).asc(),
        col(Job.id).asc(),
    )
    if job_ids:
        statement = statement.where(col(Job.id).in_(job_ids))
    if statuses:
        statement = statement.where(col(Job.status).in_(statuses))
    if stages:
        statement = statement.where(col(Job.stage).in_(stages))
    if profile is not None:
        statement = statement.where(Job.profile_name == profile)
    if all_jobs:
        statement = statement.where(
            col(Job.status).not_in([JobStatus.PROMOTED, JobStatus.SKIPPED, JobStatus.CANCELLED])
        )
    return list(session.exec(statement).all())


def select_plans_for_enqueue(
    session: Session,
    *,
    file_selectors: list[Path] | None,
    plan_selectors: list[str] | None,
    workspace_root: Path,
    resolve_path: PathResolver,
) -> list[MediaPlan]:
    if file_selectors and plan_selectors:
        raise ValueError("Use either --file or --plan selectors, not both.")
    if plan_selectors:
        plans: dict[int, MediaPlan] = {}
        for selector in plan_selectors:
            plan = find_plan(session, selector)
            if plan is None:
                raise ValueError(f"Plan not found: {selector}")
            if plan.id is not None:
                plans.setdefault(plan.id, plan)
        return sorted(plans.values(), key=lambda item: (item.created_at, item.id or 0))
    if file_selectors:
        selection = select_inventory_files(
            session,
            file_selectors=file_selectors,
            workspace_root=workspace_root,
            resolve_path=resolve_path,
        )
        if selection.missing:
            raise ValueError(f"File is not present in the media inventory: {selection.missing[0]}")
        file_plans = [
            current_plan_for_file(session, media_file) for media_file in selection.selected
        ]
        return [plan for plan in file_plans if plan is not None]
    return list(
        session.exec(
            select(MediaPlan)
            .where(
                MediaPlan.is_current == True,  # noqa: E712
                MediaPlan.is_valid == True,  # noqa: E712
            )
            .order_by(col(MediaPlan.created_at).asc(), col(MediaPlan.id).asc())
        ).all()
    )


def enqueue_plans(
    session: Session,
    plans: list[MediaPlan],
    *,
    priority: int,
    now: datetime,
) -> dict[str, int]:
    created = 0
    already_queued = 0
    already_done = 0
    stale = 0
    for plan in plans:
        media_file = session.get(MediaFile, plan.media_file_id)
        if media_file is None or _status_value(media_file.status) == MediaFileStatus.MISSING.value:
            stale += 1
            continue
        if media_file.fs_fingerprint != plan.source_fs_fingerprint:
            stale += 1
            continue
        queue_key = plan.plan_hash
        existing = find_existing_queue_job(session, queue_key=queue_key)
        if existing is not None:
            if existing.status in {
                JobStatus.PROMOTED,
                JobStatus.READY_TO_PROMOTE,
                JobStatus.SKIPPED,
                JobStatus.SIZE_REJECTED,
            }:
                already_done += 1
            else:
                already_queued += 1
            continue
        session.add(
            Job(
                media_file_id=plan.media_file_id,
                profile_name=plan.profile_name,
                profile_hash=plan.profile_hash,
                source_fs_fingerprint=plan.source_fs_fingerprint,
                queue_key=queue_key,
                probe_result_id=plan.probe_result_id,
                probe_hash=plan.probe_hash,
                plan_hash=plan.plan_hash,
                plan_path=plan.plan_path,
                output_path=plan.output_path,
                status=JobStatus.QUEUED,
                stage=JobStage.ENCODE,
                priority=priority,
                attempts=0,
                created_at=now,
                updated_at=now,
            )
        )
        created += 1
    return {
        "created": created,
        "skipped": already_queued + already_done + stale,
        "already_queued": already_queued,
        "already_done": already_done,
        "stale": stale,
    }


def _status_value(status: MediaFileStatus | str) -> str:
    if isinstance(status, MediaFileStatus):
        return status.value
    return status


def _require_id(value: object) -> int:
    identifier = getattr(value, "id", None)
    if not isinstance(identifier, int):
        raise QueueSelectionError("Expected a persisted row id.")
    return identifier
