from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from sqlalchemy.engine import make_url
from sqlmodel import Session, col, select

from avarch.config import AppConfig, resolve_data_dir, resolve_database_url
from avarch.db import create_db_engine, verify_database_revision
from avarch.db_migrations import upgrade_database
from avarch.models.db import (
    Job,
    JobAttempt,
    JobEvent,
    MediaFile,
    PromotionRecord,
    ValidationResult,
)
from avarch.models.promotion import PromotionMode
from avarch.models.scheduler import JobStage, JobStatus
from avarch.scanner import scan_root, update_inventory
from avarch.scheduler import scheduler_status
from avarch.tui.models.bootstrap import BootstrapState, BootstrapStatus
from avarch.tui.models.common import TuiError, UiRevision
from avarch.tui.models.dashboard import (
    DashboardJobSummary,
    DashboardSnapshot,
    QueueTotals,
    SchedulerSummary,
)
from avarch.tui.models.diagnostics import DoctorSnapshot
from avarch.tui.models.jobs import (
    JobAttemptSnapshot,
    JobDetailSnapshot,
    JobEventSnapshot,
    JobLogSnapshot,
    PromotionSnapshot,
    ValidationSnapshot,
)
from avarch.tui.models.profiles import (
    CopyProfileRequest,
    CopyProfileResult,
    ProfileDetailSnapshot,
    ProfileSnapshot,
    ScaffoldVpyRequest,
    ScaffoldVpyResult,
)
from avarch.tui.models.promotion import PromotionPreview, PromotionResult
from avarch.tui.models.queue import (
    QueueClearFilters,
    QueueClearPreview,
    QueueClearResult,
    QueueFilters,
    QueueJobRow,
    QueueSnapshot,
)
from avarch.tui.models.workflow import (
    AnalysisSummary,
    CandidateSnapshot,
    DirectoryEntry,
    DirectoryListing,
    EnqueueResult,
    ScanSummary,
    WorkflowPreview,
)


class TuiBackendError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SchedulerControlRequest:
    action: str
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class SchedulerControlResult:
    message: str


@dataclass(frozen=True, slots=True)
class JobActionRequest:
    job_ids: tuple[int, ...]
    action: str
    reason: str | None = None
    priority: int | None = None


@dataclass(frozen=True, slots=True)
class JobActionResult:
    message: str
    changed: int


class TuiBackend(Protocol):
    async def get_bootstrap_status(self) -> BootstrapStatus:
        ...

    async def initialize_local_state(self) -> None:
        ...

    async def get_dashboard_snapshot(self) -> DashboardSnapshot:
        ...

    async def browse_directory(self, path: Path, *, show_hidden: bool) -> DirectoryListing:
        ...

    async def scan_roots(self, roots: tuple[Path, ...]) -> ScanSummary:
        ...

    async def analyze_media(self, media_file_ids: tuple[int, ...]) -> AnalysisSummary:
        ...

    async def list_workflow_candidates(self, roots: tuple[Path, ...]) -> CandidateSnapshot:
        ...

    async def preview_workflow(
        self,
        *,
        media_file_ids: tuple[int, ...],
        profile_name: str,
    ) -> WorkflowPreview:
        ...

    async def enqueue_workflow(
        self,
        *,
        media_file_ids: tuple[int, ...],
        profile_name: str,
        priority: int,
    ) -> EnqueueResult:
        ...

    async def get_queue_snapshot(self, filters: QueueFilters) -> QueueSnapshot:
        ...

    async def get_job_detail(self, job_id: int) -> JobDetailSnapshot:
        ...

    async def get_job_log_tail(
        self,
        *,
        job_id: int,
        attempt_number: int | None,
        tail_bytes: int,
    ) -> JobLogSnapshot:
        ...

    async def request_scheduler_control(
        self,
        request: SchedulerControlRequest,
    ) -> SchedulerControlResult:
        ...

    async def perform_job_action(self, request: JobActionRequest) -> JobActionResult:
        ...

    async def preview_queue_clear(self, filters: QueueClearFilters) -> QueueClearPreview:
        ...

    async def confirm_queue_clear(self, preview: QueueClearPreview) -> QueueClearResult:
        ...

    async def list_profiles(self) -> ProfileSnapshot:
        ...

    async def get_profile_detail(self, profile_name: str) -> ProfileDetailSnapshot:
        ...

    async def copy_profile(self, request: CopyProfileRequest) -> CopyProfileResult:
        ...

    async def scaffold_vpy(self, request: ScaffoldVpyRequest) -> ScaffoldVpyResult:
        ...

    async def preview_promotion(
        self,
        *,
        job_id: int,
        mode: PromotionMode,
    ) -> PromotionPreview:
        ...

    async def execute_promotion(
        self,
        *,
        job_id: int,
        mode: PromotionMode,
    ) -> PromotionResult:
        ...

    async def recover_promotion(self, *, job_id: int) -> PromotionResult:
        ...

    async def run_doctor(self) -> DoctorSnapshot:
        ...


class LocalTuiBackend:
    def __init__(self, *, config: AppConfig, config_path: Path) -> None:
        self.config = config
        self.config_path = config_path
        self.data_dir = resolve_data_dir(config, config_path)
        self.database_url = resolve_database_url(config, config_path)

    async def get_bootstrap_status(self) -> BootstrapStatus:
        return await asyncio.to_thread(self._get_bootstrap_status_sync)

    async def initialize_local_state(self) -> None:
        await asyncio.to_thread(self._initialize_local_state_sync)

    async def get_dashboard_snapshot(self) -> DashboardSnapshot:
        return await asyncio.to_thread(self._get_dashboard_snapshot_sync)

    async def browse_directory(self, path: Path, *, show_hidden: bool) -> DirectoryListing:
        return await asyncio.to_thread(self._browse_directory_sync, path, show_hidden)

    async def scan_roots(self, roots: tuple[Path, ...]) -> ScanSummary:
        return await asyncio.to_thread(self._scan_roots_sync, roots)

    async def get_queue_snapshot(self, filters: QueueFilters) -> QueueSnapshot:
        return await asyncio.to_thread(self._get_queue_snapshot_sync, filters)

    async def get_job_detail(self, job_id: int) -> JobDetailSnapshot:
        return await asyncio.to_thread(self._get_job_detail_sync, job_id)

    def _get_bootstrap_status_sync(self) -> BootstrapStatus:
        database_path = _sqlite_database_path(self.database_url)
        if database_path is not None and not database_path.exists():
            return BootstrapStatus(
                state=BootstrapState.DATABASE_MISSING,
                config_path=self.config_path,
                data_dir=self.data_dir,
                database_url=self.database_url,
            )

        try:
            engine = create_db_engine(self.database_url)
            verify_database_revision(engine)
        except Exception as exc:
            return BootstrapStatus(
                state=BootstrapState.SCHEMA_ERROR,
                config_path=self.config_path,
                data_dir=self.data_dir,
                database_url=self.database_url,
                error=TuiError(
                    title="Local state cannot be used",
                    summary=str(exc).splitlines()[0],
                    details=str(exc),
                ),
            )

        return BootstrapStatus(
            state=BootstrapState.READY,
            config_path=self.config_path,
            data_dir=self.data_dir,
            database_url=self.database_url,
        )

    def _initialize_local_state_sync(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        upgrade_database(self.database_url)

    def _get_dashboard_snapshot_sync(self) -> DashboardSnapshot:
        engine = create_db_engine(self.database_url)
        now = _utc_now()
        with Session(engine) as session:
            revision = _build_revision(session)
            scheduler = _scheduler_summary(session, now=now)
            totals = _queue_totals(session, now=now)
            media_by_id = _media_by_id(session)
            active_jobs = tuple(
                _dashboard_job_summary(job, media_by_id)
                for job in _ordered_jobs(session, status=JobStatus.RUNNING, limit=5)
            )
            recent_failures = tuple(
                _dashboard_job_summary(job, media_by_id)
                for job in _ordered_jobs(session, status=JobStatus.FAILED, limit=5)
            )
            promotion_ready = tuple(
                _dashboard_job_summary(job, media_by_id)
                for job in _promotion_ready_jobs(session, limit=5)
            )

        return DashboardSnapshot(
            revision=revision,
            scheduler=scheduler,
            queue_totals=totals,
            active_jobs=active_jobs,
            recent_failures=recent_failures,
            promotion_ready=promotion_ready,
        )

    def _browse_directory_sync(self, path: Path, show_hidden: bool) -> DirectoryListing:
        resolved = path.expanduser().resolve()
        if not resolved.is_dir():
            raise TuiBackendError(f"Not a directory: {resolved}")

        entries: list[DirectoryEntry] = []
        for entry in sorted(resolved.iterdir(), key=lambda item: (not item.is_dir(), item.name)):
            is_hidden = entry.name.startswith(".")
            if is_hidden and not show_hidden:
                continue
            entries.append(
                DirectoryEntry(
                    path=entry,
                    name=entry.name,
                    is_dir=entry.is_dir(),
                    is_hidden=is_hidden,
                )
            )

        return DirectoryListing(
            path=resolved,
            parent=resolved.parent if resolved.parent != resolved else None,
            entries=tuple(entries),
        )

    def _scan_roots_sync(self, roots: tuple[Path, ...]) -> ScanSummary:
        engine = create_db_engine(self.database_url)
        added = 0
        changed = 0
        missing = 0
        unchanged = 0
        for root in roots:
            snapshots = scan_root(
                root,
                extensions=self.config.scanner.extensions,
                exclude_directories=self.config.scanner.exclude_directories,
            )
            with Session(engine) as session, session.begin():
                result = update_inventory(
                    session,
                    root=root,
                    snapshots=snapshots,
                    scanned_at=_utc_now(),
                )
            added += result.added
            changed += result.changed
            missing += result.missing
            unchanged += result.unchanged

        return ScanSummary(
            roots=roots,
            added=added,
            changed=changed,
            missing=missing,
            unchanged=unchanged,
        )

    def _get_queue_snapshot_sync(self, filters: QueueFilters) -> QueueSnapshot:
        engine = create_db_engine(self.database_url)
        now = _utc_now()
        with Session(engine) as session:
            revision = _build_revision(session)
            scheduler = _scheduler_summary(session, now=now)
            totals = _queue_totals(session, now=now)
            media_by_id = _media_by_id(session)
            jobs = list(
                session.exec(
                    select(Job).order_by(
                        col(Job.priority).desc(),
                        col(Job.created_at).asc(),
                        col(Job.id).asc(),
                    )
                ).all()
            )
            rows = tuple(
                _queue_job_row(job, media_by_id)
                for job in _filter_jobs(jobs, media_by_id, filters)
            )

        return QueueSnapshot(
            revision=revision,
            scheduler=scheduler,
            totals=totals,
            filters=filters,
            rows=rows,
        )

    def _get_job_detail_sync(self, job_id: int) -> JobDetailSnapshot:
        engine = create_db_engine(self.database_url)
        with Session(engine) as session:
            job = session.get(Job, job_id)
            if job is None:
                raise TuiBackendError(f"Job not found: {job_id}")
            media_file = session.get(MediaFile, job.media_file_id)
            if media_file is None:
                raise TuiBackendError(f"Media file not found for job: {job_id}")
            revision = _build_revision(session)
            attempts = tuple(
                _attempt_snapshot(attempt)
                for attempt in session.exec(
                    select(JobAttempt)
                    .where(JobAttempt.job_id == job_id)
                    .order_by(col(JobAttempt.attempt_number))
                ).all()
            )
            events = tuple(
                _event_snapshot(event)
                for event in session.exec(
                    select(JobEvent)
                    .where(JobEvent.job_id == job_id)
                    .order_by(col(JobEvent.created_at).desc(), col(JobEvent.id).desc())
                ).all()
            )
            validation_result = (
                session.get(ValidationResult, job.latest_validation_id)
                if job.latest_validation_id is not None
                else None
            )
            promotion_record = (
                session.get(PromotionRecord, job.latest_promotion_id)
                if job.latest_promotion_id is not None
                else None
            )
            validation = _validation_snapshot(validation_result)
            promotion = _promotion_snapshot(promotion_record)

            return JobDetailSnapshot(
                revision=revision,
                job_id=_require_id(job.id, "job"),
                media_file_id=job.media_file_id,
                file_name=Path(media_file.path).name,
                source_path=media_file.path,
                status=_value(job.status),
                stage=_value(job.stage),
                profile_name=job.profile_name,
                profile_hash=job.profile_hash,
                priority=job.priority,
                attempts_count=job.attempts,
                queue_key=job.queue_key,
                source_fs_fingerprint=job.source_fs_fingerprint,
                probe_hash=job.probe_hash,
                plan_hash=job.plan_hash,
                plan_path=job.plan_path,
                output_path=job.output_path,
                last_error_type=job.last_error_type,
                last_error_message=job.last_error_message,
                created_at=job.created_at,
                updated_at=job.updated_at,
                started_at=job.started_at,
                finished_at=job.finished_at,
                attempts=attempts,
                events=events,
                latest_validation=validation,
                latest_promotion=promotion,
            )


def _sqlite_database_path(database_url: str) -> Path | None:
    url = make_url(database_url)
    database = url.database
    if not url.drivername.startswith("sqlite") or database is None or database in {"", ":memory:"}:
        return None
    return Path(database)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _value(value: object) -> str:
    enum_value = getattr(value, "value", None)
    return str(enum_value if enum_value is not None else value)


def _require_id(value: int | None, entity: str) -> int:
    if value is None:
        raise TuiBackendError(f"{entity} has no database id")
    return value


def _media_by_id(session: Session) -> dict[int, MediaFile]:
    return {
        media_file.id: media_file
        for media_file in session.exec(select(MediaFile)).all()
        if media_file.id is not None
    }


def _build_revision(session: Session) -> UiRevision:
    jobs = list(session.exec(select(Job)).all())
    attempts = list(session.exec(select(JobAttempt)).all())
    validations = list(session.exec(select(ValidationResult)).all())
    promotions = list(session.exec(select(PromotionRecord)).all())
    return UiRevision(
        scheduler_generation=scheduler_status(session, now=_utc_now()).control_generation,
        newest_job_updated_at=_max_datetime(job.updated_at for job in jobs),
        newest_attempt_updated_at=_max_datetime(attempt.finished_at for attempt in attempts),
        newest_validation_created_at=_max_datetime(
            validation.created_at for validation in validations
        ),
        newest_promotion_updated_at=_max_datetime(promotion.updated_at for promotion in promotions),
    )


def _max_datetime(values: Iterable[datetime | None]) -> datetime | None:
    datetimes = [value for value in values if isinstance(value, datetime)]
    return max(datetimes) if datetimes else None


def _scheduler_summary(session: Session, *, now: datetime) -> SchedulerSummary:
    status = scheduler_status(session, now=now)
    return SchedulerSummary(
        mode=_value(status.mode),
        lease_state=status.lease_state,
        runner_id=status.runner_id,
        heartbeat_at=status.heartbeat_at,
        lease_expires_at=status.lease_expires_at,
        control_generation=status.control_generation,
        acknowledged_generation=status.acknowledged_generation,
        cancel_pending=status.cancel_pending,
        hold_pending=status.hold_pending,
    )


def _queue_totals(session: Session, *, now: datetime) -> QueueTotals:
    counts = scheduler_status(session, now=now).counts_by_status
    return QueueTotals(
        pending=counts[JobStatus.PENDING],
        running=counts[JobStatus.RUNNING],
        held=counts[JobStatus.HELD],
        failed=counts[JobStatus.FAILED],
        validated=counts[JobStatus.VALIDATED],
        canceled=counts[JobStatus.CANCELED],
        completed=counts[JobStatus.COMPLETED],
        skipped=counts[JobStatus.SKIPPED],
    )


def _ordered_jobs(session: Session, *, status: JobStatus, limit: int) -> list[Job]:
    return list(
        session.exec(
            select(Job)
            .where(Job.status == status)
            .order_by(col(Job.updated_at).desc(), col(Job.id).desc())
            .limit(limit)
        ).all()
    )


def _promotion_ready_jobs(session: Session, *, limit: int) -> list[Job]:
    return list(
        session.exec(
            select(Job)
            .where(Job.status == JobStatus.VALIDATED, Job.stage == JobStage.PROMOTE)
            .order_by(col(Job.updated_at).desc(), col(Job.id).desc())
            .limit(limit)
        ).all()
    )


def _dashboard_job_summary(
    job: Job,
    media_by_id: dict[int, MediaFile],
) -> DashboardJobSummary:
    media_file = media_by_id.get(job.media_file_id)
    source_path = media_file.path if media_file is not None else "<missing>"
    return DashboardJobSummary(
        job_id=_require_id(job.id, "job"),
        file_name=Path(source_path).name,
        source_path=source_path,
        status=_value(job.status),
        stage=_value(job.stage),
        profile_name=job.profile_name,
        priority=job.priority,
        updated_at=job.updated_at,
        error_message=job.last_error_message,
    )


def _filter_jobs(
    jobs: list[Job],
    media_by_id: dict[int, MediaFile],
    filters: QueueFilters,
) -> list[Job]:
    filtered: list[Job] = []
    search_text = filters.search_text.lower() if filters.search_text else None
    for job in jobs:
        media_file = media_by_id.get(job.media_file_id)
        source_path = media_file.path if media_file is not None else ""
        if filters.statuses is not None and JobStatus(job.status) not in filters.statuses:
            continue
        if filters.stages is not None and JobStage(job.stage) not in filters.stages:
            continue
        if filters.profile_name is not None and job.profile_name != filters.profile_name:
            continue
        if filters.active_only and JobStatus(job.status) != JobStatus.RUNNING:
            continue
        if filters.failures_only and JobStatus(job.status) != JobStatus.FAILED:
            continue
        if filters.promotion_ready_only and not (
            JobStatus(job.status) == JobStatus.VALIDATED and JobStage(job.stage) == JobStage.PROMOTE
        ):
            continue
        if search_text is not None and search_text not in source_path.lower():
            continue
        filtered.append(job)
        if filters.limit is not None and len(filtered) >= filters.limit:
            break
    return filtered


def _queue_job_row(job: Job, media_by_id: dict[int, MediaFile]) -> QueueJobRow:
    media_file = media_by_id.get(job.media_file_id)
    source_path = media_file.path if media_file is not None else "<missing>"
    return QueueJobRow(
        job_id=_require_id(job.id, "job"),
        status=_value(job.status).upper(),
        stage=_value(job.stage),
        priority=job.priority,
        attempts=job.attempts,
        progress=_value(job.stage),
        profile_name=job.profile_name,
        file_name=Path(source_path).name,
        source_path=source_path,
        updated_at=job.updated_at,
        control=_job_control_label(job),
    )


def _job_control_label(job: Job) -> str | None:
    if job.cancel_requested_at is not None and JobStatus(job.status) != JobStatus.CANCELED:
        return "cancel requested"
    if job.hold_requested_at is not None and JobStatus(job.status) != JobStatus.HELD:
        return "hold requested"
    if JobStatus(job.status) == JobStatus.HELD:
        return "held"
    return None


def _attempt_snapshot(attempt: JobAttempt) -> JobAttemptSnapshot:
    return JobAttemptSnapshot(
        attempt_id=_require_id(attempt.id, "attempt"),
        attempt_number=attempt.attempt_number,
        stage=_value(attempt.stage),
        resource_class=_value(attempt.resource_class),
        status=_value(attempt.status),
        runner_id=attempt.runner_id,
        started_at=attempt.started_at,
        finished_at=attempt.finished_at,
        stdout_log=attempt.stdout_log,
        stderr_log=attempt.stderr_log,
        output_path=attempt.output_path,
        error_type=attempt.error_type,
        error_message=attempt.error_message,
    )


def _event_snapshot(event: JobEvent) -> JobEventSnapshot:
    return JobEventSnapshot(
        event_id=_require_id(event.id, "event"),
        event_type=_value(event.event_type),
        actor=event.actor,
        reason=event.reason,
        details_json=event.details_json,
        created_at=event.created_at,
    )


def _validation_snapshot(result: ValidationResult | None) -> ValidationSnapshot | None:
    if result is None:
        return None
    return ValidationSnapshot(
        validation_id=_require_id(result.id, "validation"),
        passed=result.passed,
        output_path=result.output_path,
        plan_hash=result.plan_hash,
        policy_hash=result.policy_hash,
        created_at=result.created_at,
    )


def _promotion_snapshot(record: PromotionRecord | None) -> PromotionSnapshot | None:
    if record is None:
        return None
    return PromotionSnapshot(
        promotion_id=_require_id(record.id, "promotion"),
        mode=_value(record.mode),
        status=_value(record.status),
        phase=_value(record.phase),
        source_path=record.source_path,
        final_path=record.final_path,
        updated_at=record.updated_at,
    )
