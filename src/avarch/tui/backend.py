from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, cast

from sqlalchemy import inspect
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
    MediaFileStatus,
    ProbeResult,
    PromotionRecord,
    ValidationResult,
)
from avarch.models.plan import TranscodePlan
from avarch.models.probe import NormalizedProbe
from avarch.models.promotion import PromotionMode
from avarch.models.scheduler import JobStage, JobStatus
from avarch.planner import PlanningError, build_plan, build_profile_hash, load_planning_context
from avarch.probe import (
    ProbeError,
    normalize_probe,
    parse_normalized_probe_json,
    run_ffprobe,
    store_probe_result,
)
from avarch.profiles.registry import (
    ProfileOrigin,
    ProfileRegistry,
    ProfileRegistryError,
    ResolvedProfile,
)
from avarch.scanner import scan_root, update_inventory
from avarch.scheduler import enqueue_inventory, scheduler_status
from avarch.serialization import canonical_json
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
from avarch.tui.models.library import LibraryProbeState, LibraryRow, LibrarySnapshot
from avarch.tui.models.profiles import (
    CopyProfileRequest,
    CopyProfileResult,
    ProfileDetailSnapshot,
    ProfileRow,
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
    AnalysisFailure,
    AnalysisSummary,
    CandidateRow,
    CandidateSnapshot,
    CandidateState,
    DirectoryEntry,
    DirectoryListing,
    EnqueueResult,
    ScanSummary,
    WorkflowPreview,
    WorkflowPreviewRow,
)
from avarch.vapoursynth import resolve_vapoursynth_template

ProbeRunner = Callable[[Path], Mapping[str, Any]]


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

    async def list_library(self) -> LibrarySnapshot:
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
    def __init__(
        self,
        *,
        config: AppConfig,
        config_path: Path,
        probe_runner: ProbeRunner = run_ffprobe,
    ) -> None:
        self.config = config
        self.config_path = config_path
        self.data_dir = resolve_data_dir(config, config_path)
        self.database_url = resolve_database_url(config, config_path)
        self.probe_runner = probe_runner

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

    async def list_workflow_candidates(self, roots: tuple[Path, ...]) -> CandidateSnapshot:
        return await asyncio.to_thread(self._list_workflow_candidates_sync, roots)

    async def analyze_media(self, media_file_ids: tuple[int, ...]) -> AnalysisSummary:
        return await asyncio.to_thread(self._analyze_media_sync, media_file_ids)

    async def list_library(self) -> LibrarySnapshot:
        return await asyncio.to_thread(self._list_library_sync)

    async def list_profiles(self) -> ProfileSnapshot:
        return await asyncio.to_thread(self._list_profiles_sync)

    async def get_profile_detail(self, profile_name: str) -> ProfileDetailSnapshot:
        return await asyncio.to_thread(self._get_profile_detail_sync, profile_name)

    async def preview_workflow(
        self,
        *,
        media_file_ids: tuple[int, ...],
        profile_name: str,
    ) -> WorkflowPreview:
        return await asyncio.to_thread(
            self._preview_workflow_sync,
            media_file_ids,
            profile_name,
        )

    async def enqueue_workflow(
        self,
        *,
        media_file_ids: tuple[int, ...],
        profile_name: str,
        priority: int,
    ) -> EnqueueResult:
        return await asyncio.to_thread(
            self._enqueue_workflow_sync,
            media_file_ids,
            profile_name,
            priority,
        )

    def _get_bootstrap_status_sync(self) -> BootstrapStatus:
        try:
            ProfileRegistry.from_config(self.config)
        except ProfileRegistryError as exc:
            return BootstrapStatus(
                state=BootstrapState.PROFILE_REGISTRY_ERROR,
                config_path=self.config_path,
                data_dir=self.data_dir,
                database_url=self.database_url,
                error=TuiError(
                    title="Profiles could not be loaded",
                    summary=str(exc).splitlines()[0],
                    details=str(exc),
                ),
            )

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
            tables = set(inspect(engine).get_table_names())
            if not tables:
                return BootstrapStatus(
                    state=BootstrapState.DATABASE_UNINITIALIZED,
                    config_path=self.config_path,
                    data_dir=self.data_dir,
                    database_url=self.database_url,
                )
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

    def _list_workflow_candidates_sync(self, roots: tuple[Path, ...]) -> CandidateSnapshot:
        engine = create_db_engine(self.database_url)
        normalized_roots = tuple(root.resolve() for root in roots)
        with Session(engine) as session:
            media_files = list(
                session.exec(select(MediaFile).order_by(col(MediaFile.path))).all()
            )
            rows = tuple(
                _candidate_row(session, media_file)
                for media_file in media_files
                if _media_in_roots(media_file, normalized_roots)
            )
        return CandidateSnapshot(rows=rows)

    def _analyze_media_sync(self, media_file_ids: tuple[int, ...]) -> AnalysisSummary:
        completed = 0
        failures: list[AnalysisFailure] = []
        for media_file_id in media_file_ids:
            media_file = self._load_media_file(media_file_id)
            if media_file is None:
                failures.append(
                    AnalysisFailure(
                        media_file_id=media_file_id,
                        path="<missing>",
                        error="Media file is no longer in the inventory.",
                    )
                )
                continue

            try:
                raw_probe = self.probe_runner(Path(media_file.path))
                normalized_probe = normalize_probe(raw_probe)
                self._store_analysis_result(
                    media_file_id=media_file_id,
                    raw_probe=raw_probe,
                    normalized_probe=normalized_probe,
                )
            except Exception as exc:
                failures.append(
                    AnalysisFailure(
                        media_file_id=media_file_id,
                        path=media_file.path,
                        error=_bounded_error(str(exc) or exc.__class__.__name__),
                    )
                )
                continue
            completed += 1

        return AnalysisSummary(
            requested=len(media_file_ids),
            completed=completed,
            failed=len(failures),
            failures=tuple(failures),
        )

    def _load_media_file(self, media_file_id: int) -> MediaFile | None:
        engine = create_db_engine(self.database_url)
        with Session(engine) as session:
            return session.get(MediaFile, media_file_id)

    def _store_analysis_result(
        self,
        *,
        media_file_id: int,
        raw_probe: Mapping[str, Any],
        normalized_probe: NormalizedProbe,
    ) -> None:
        engine = create_db_engine(self.database_url)
        with Session(engine) as session, session.begin():
            media_file = session.get(MediaFile, media_file_id)
            if media_file is None:
                raise TuiBackendError("Media file is no longer in the inventory.")
            store_probe_result(
                session,
                media_file=media_file,
                raw_probe=raw_probe,
                normalized_probe=normalized_probe,
                created_at=_utc_now(),
            )

    def _list_library_sync(self) -> LibrarySnapshot:
        engine = create_db_engine(self.database_url)
        with Session(engine) as session:
            media_files = list(
                session.exec(select(MediaFile).order_by(col(MediaFile.path))).all()
            )
            rows = tuple(_library_row(session, media_file) for media_file in media_files)
        return LibrarySnapshot(rows=rows)

    def _list_profiles_sync(self) -> ProfileSnapshot:
        registry = ProfileRegistry.from_config(self.config)
        return ProfileSnapshot(
            profiles=tuple(_profile_row(profile) for profile in registry.list_profiles())
        )

    def _get_profile_detail_sync(self, profile_name: str) -> ProfileDetailSnapshot:
        registry = ProfileRegistry.from_config(self.config)
        profile = registry.get(profile_name)
        row = _profile_row(profile)
        return ProfileDetailSnapshot(
            profile=row,
            definition_hash=_profile_definition_hash(profile),
            vapoursynth_mode=row.vapoursynth_mode,
            explanation=_profile_explanation(profile),
            known_limitations=row.known_limitations,
        )

    def _preview_workflow_sync(
        self,
        media_file_ids: tuple[int, ...],
        profile_name: str,
    ) -> WorkflowPreview:
        registry = ProfileRegistry.from_config(self.config)
        resolved_profile = registry.get(profile_name)
        profile_row = _profile_row(resolved_profile)
        resolved_template = resolve_vapoursynth_template(resolved_profile.profile)
        engine = create_db_engine(self.database_url)
        rows: list[WorkflowPreviewRow] = []
        with Session(engine) as session:
            for media_file_id in media_file_ids:
                media_file = session.get(MediaFile, media_file_id)
                if media_file is None:
                    rows.append(
                        _blocked_preview_row(
                            media_file_id,
                            "<missing>",
                            "Not in inventory.",
                        )
                    )
                    continue
                if media_file.latest_probe_id is None:
                    rows.append(
                        _needs_analysis_preview_row(
                            media_file_id,
                            media_file.path,
                            "No current probe is available.",
                        )
                    )
                    continue
                try:
                    context = load_planning_context(
                        session,
                        input_path=Path(media_file.path),
                        resolved_profile=resolved_profile,
                    )
                    plan = build_plan(
                        context,
                        data_dir=self.data_dir,
                        resolved_template=resolved_template,
                    )
                except PlanningError as exc:
                    message = str(exc) or exc.__class__.__name__
                    if "probe" in message.lower():
                        rows.append(
                            _needs_analysis_preview_row(
                                media_file_id,
                                media_file.path,
                                message,
                            )
                        )
                    else:
                        rows.append(_blocked_preview_row(media_file_id, media_file.path, message))
                    continue
                rows.append(_planned_preview_row(plan))

        preview_rows = tuple(rows)
        return WorkflowPreview(
            media_file_ids=media_file_ids,
            profile_name=profile_name,
            profile_effective_hash=profile_row.effective_hash,
            summary=_workflow_preview_summary(profile_row, preview_rows),
            rows=preview_rows,
        )

    def _enqueue_workflow_sync(
        self,
        media_file_ids: tuple[int, ...],
        profile_name: str,
        priority: int,
    ) -> EnqueueResult:
        engine = create_db_engine(self.database_url)
        with Session(engine) as session, session.begin():
            summary = enqueue_inventory(
                session,
                config=self.config,
                profile_name=profile_name,
                priority=priority,
                now=_utc_now(),
                media_file_ids=media_file_ids,
            )
        return EnqueueResult(
            created=summary.created,
            already_queued=summary.existing,
            already_completed=0,
            not_eligible=summary.missing_skipped,
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


class StaticBootstrapBackend:
    def __init__(self, status: BootstrapStatus) -> None:
        self.status = status
        self.initialize_calls = 0

    async def get_bootstrap_status(self) -> BootstrapStatus:
        return self.status

    async def initialize_local_state(self) -> None:
        self.initialize_calls += 1
        self.status = BootstrapStatus(
            state=BootstrapState.READY,
            config_path=self.status.config_path,
            data_dir=self.status.data_dir,
            database_url=self.status.database_url,
        )

    async def get_dashboard_snapshot(self) -> DashboardSnapshot:
        revision = UiRevision(
            scheduler_generation=0,
            newest_job_updated_at=None,
            newest_attempt_updated_at=None,
            newest_validation_created_at=None,
            newest_promotion_updated_at=None,
        )
        return DashboardSnapshot(
            revision=revision,
            scheduler=SchedulerSummary(
                mode="unknown",
                lease_state="inactive",
                runner_id=None,
                heartbeat_at=None,
                lease_expires_at=None,
                control_generation=0,
                acknowledged_generation=0,
                cancel_pending=0,
                hold_pending=0,
            ),
            queue_totals=QueueTotals(),
            active_jobs=(),
            recent_failures=(),
            promotion_ready=(),
        )


def _bounded_error(error: str, *, limit: int = 1000) -> str:
    return error if len(error) <= limit else f"{error[:limit]}..."


def _profile_row(profile: ResolvedProfile) -> ProfileRow:
    template = resolve_vapoursynth_template(profile.profile)
    template_hash = template.template_hash if template is not None else None
    origin = profile.origin.value
    return ProfileRow(
        name=profile.name,
        origin=origin,
        description=profile.document.description or "",
        source_path=_profile_source_path(profile),
        effective_hash=build_profile_hash(profile.profile, template_hash=template_hash),
        tags=tuple(profile.document.tags),
        vapoursynth_mode="custom_template" if template is not None else "generated",
        video_summary=_profile_video_summary(profile),
        audio_summary=_profile_audio_summary(profile),
        subtitle_summary=_profile_subtitle_summary(profile),
        known_limitations=tuple(profile.document.known_limitations),
        is_builtin_starting_point=profile.origin == ProfileOrigin.BUILTIN,
    )


def _profile_source_path(profile: ResolvedProfile) -> Path | None:
    if profile.origin == ProfileOrigin.BUILTIN:
        return None
    return Path(profile.source)


def _profile_definition_hash(profile: ResolvedProfile) -> str:
    payload = canonical_json(profile.document.model_dump(mode="json"))
    return hashlib.blake2b(
        b"tui-profile-definition-v1\0" + payload.encode("utf-8"),
        digest_size=32,
    ).hexdigest()


def _profile_explanation(profile: ResolvedProfile) -> str:
    row = _profile_row(profile)
    lines = [
        (
            f"{profile.name} uses the {profile.profile.backend} backend "
            f"and writes {profile.profile.container} files."
        ),
        row.video_summary,
        row.audio_summary,
        row.subtitle_summary,
    ]
    if row.is_builtin_starting_point:
        lines.append("This built-in profile is a reference starting point for customization.")
    return "\n".join(line for line in lines if line)


def _profile_video_summary(profile: ResolvedProfile) -> str:
    video = profile.profile.video
    av1an = profile.profile.av1an
    hdr_policy = "HDR to SDR enabled" if video.hdr_to_sdr else "HDR is preserved"
    return (
        f"{av1an.encoder.upper()} {av1an.video_args}; "
        f"maximum width {video.max_width}; {hdr_policy}; 10-bit 4:2:0 output."
    )


def _profile_audio_summary(profile: ResolvedProfile) -> str:
    audio = profile.profile.audio
    languages = ", ".join(audio.languages) if audio.languages else "all configured languages"
    return (
        f"Preferred {languages} audio; {audio.channels} channel "
        f"{audio.codec} at {audio.bitrate}."
    )


def _profile_subtitle_summary(profile: ResolvedProfile) -> str:
    subtitles = profile.profile.subtitles
    languages = (
        ", ".join(subtitles.languages) if subtitles.languages else "all configured languages"
    )
    forced = "keeps forced subtitles" if subtitles.keep_forced else "does not force-keep subtitles"
    return f"Subtitles: {languages}; {forced}."


def _planned_preview_row(plan: TranscodePlan) -> WorkflowPreviewRow:
    return WorkflowPreviewRow(
        media_file_id=plan.media_file_id,
        path=str(plan.input_path),
        result="ENCODE",
        video=_preview_video_summary(plan),
        audio=_preview_audio_summary(plan),
        subtitles=_preview_subtitle_summary(plan),
        reason="Plan can be created with the selected profile.",
        probe_hash=plan.probe_hash,
        source_fs_fingerprint=plan.source_fs_fingerprint,
    )


def _needs_analysis_preview_row(
    media_file_id: int,
    path: str,
    reason: str,
) -> WorkflowPreviewRow:
    return WorkflowPreviewRow(
        media_file_id=media_file_id,
        path=path,
        result="NEEDS ANALYSIS",
        video="Metadata required",
        audio="Metadata required",
        subtitles="Metadata required",
        reason=reason,
    )


def _blocked_preview_row(media_file_id: int, path: str, reason: str) -> WorkflowPreviewRow:
    return WorkflowPreviewRow(
        media_file_id=media_file_id,
        path=path,
        result="BLOCKED",
        video="No plan",
        audio="No plan",
        subtitles="No plan",
        reason=reason,
    )


def _preview_video_summary(plan: TranscodePlan) -> str:
    video = plan.video
    dimensions = (
        f"{video.source_width}x{video.source_height} -> "
        f"{video.target_width}x{video.target_height}"
        if video.resize_required
        else f"{video.source_width}x{video.source_height}"
    )
    hdr = "; HDR to SDR" if video.hdr_to_sdr else ""
    return f"{dimensions} {video.source_codec} -> AV1{hdr}"


def _preview_audio_summary(plan: TranscodePlan) -> str:
    audio = plan.audio
    language = audio.source_language or "unknown language"
    source = audio.source_codec or "unknown codec"
    return (
        f"{language} {source} -> {audio.target_codec} "
        f"{audio.target_channels}ch {audio.target_bitrate}"
    )


def _preview_subtitle_summary(plan: TranscodePlan) -> str:
    if not plan.subtitles.streams:
        return "None selected"
    labels: list[str] = []
    for stream in plan.subtitles.streams:
        language = stream.language or "unknown"
        forced = " forced" if stream.forced else ""
        labels.append(f"{language}{forced}")
    return "Keep " + ", ".join(labels)


def _workflow_preview_summary(
    profile: ProfileRow,
    rows: tuple[WorkflowPreviewRow, ...],
) -> str:
    encodable = sum(1 for row in rows if row.result == "ENCODE")
    needs_analysis = sum(1 for row in rows if row.result == "NEEDS ANALYSIS")
    blocked = sum(1 for row in rows if row.result == "BLOCKED")
    return "\n".join(
        [
            "This workflow will:",
            f"1. Review {len(rows)} selected file(s) with profile {profile.name}.",
            f"2. Create in-memory encode plans for {encodable} file(s).",
            "3. Encode video to 10-bit AV1 with the selected SVT-AV1 settings.",
            "4. Apply the profile audio policy and mux selected subtitles.",
            "5. Validate duration, streams, codec, resolution, and output size.",
            "6. Wait for your manual approval before promotion.",
            f"Needs analysis: {needs_analysis}",
            f"Blocked: {blocked}",
        ]
    )


def _sqlite_database_path(database_url: str) -> Path | None:
    url = make_url(database_url)
    database = url.database
    if (
        not url.drivername.startswith("sqlite")
        or database is None
        or database in {"", ":memory:"}
    ):
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
        newest_promotion_updated_at=_max_datetime(
            promotion.updated_at for promotion in promotions
        ),
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


def _media_in_roots(media_file: MediaFile, roots: tuple[Path, ...]) -> bool:
    if not roots:
        return True
    path = Path(media_file.path).resolve()
    for root in roots:
        try:
            path.relative_to(root)
        except ValueError:
            continue
        return True
    return False


def _library_row(session: Session, media_file: MediaFile) -> LibraryRow:
    media_file_id = _require_id(media_file.id, "media file")
    probe = (
        session.get(ProbeResult, media_file.latest_probe_id)
        if media_file.latest_probe_id is not None
        else None
    )
    probe_state = _library_probe_state(media_file, probe)
    container: str | None = None
    video_codec: str | None = None
    resolution: str | None = None
    duration_seconds: float | None = None
    if probe is not None and probe_state == LibraryProbeState.CURRENT:
        try:
            normalized_probe = parse_normalized_probe_json(probe.normalized_json)
        except ProbeError:
            probe_state = LibraryProbeState.UNREADABLE
        else:
            duration_seconds = normalized_probe.duration_seconds
            if normalized_probe.video_streams:
                video = min(normalized_probe.video_streams, key=lambda stream: stream.index)
                video_codec = video.codec
                if video.width is not None and video.height is not None:
                    resolution = f"{video.width}x{video.height}"
            container = _probe_container(probe)

    return LibraryRow(
        media_file_id=media_file_id,
        path=media_file.path,
        inventory_status=_media_file_status_value(media_file.status),
        probe_state=probe_state,
        container=container,
        video_codec=video_codec,
        resolution=resolution,
        duration_seconds=duration_seconds,
        last_scanned_at=media_file.last_seen_at.isoformat(),
        selectable=_media_file_status_value(media_file.status) != MediaFileStatus.MISSING.value,
    )


def _library_probe_state(
    media_file: MediaFile,
    probe: ProbeResult | None,
) -> LibraryProbeState:
    if probe is None:
        return LibraryProbeState.MISSING
    if probe.source_fs_fingerprint != media_file.fs_fingerprint:
        return LibraryProbeState.STALE
    return LibraryProbeState.CURRENT


def _probe_container(probe: ProbeResult) -> str | None:
    try:
        raw_payload: object = json.loads(probe.ffprobe_json)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw_payload, dict):
        return None
    payload = cast(dict[str, object], raw_payload)
    format_payload = payload.get("format")
    if not isinstance(format_payload, dict):
        return None
    format_data = cast(dict[str, object], format_payload)
    format_name = format_data.get("format_name")
    return str(format_name) if format_name is not None else None


def _candidate_row(session: Session, media_file: MediaFile) -> CandidateRow:
    media_file_id = _require_id(media_file.id, "media file")
    status = _media_file_status_value(media_file.status)
    if status == MediaFileStatus.MISSING.value:
        return CandidateRow(
            media_file_id=media_file_id,
            path=media_file.path,
            state=CandidateState.EXCLUDED,
            reason="File is marked missing in the inventory.",
            eligible=False,
        )

    if media_file.latest_probe_id is None:
        return CandidateRow(
            media_file_id=media_file_id,
            path=media_file.path,
            state=CandidateState.NEEDS_ANALYSIS,
            reason="No current probe is available.",
            eligible=True,
        )

    probe = session.get(ProbeResult, media_file.latest_probe_id)
    if probe is None or probe.source_fs_fingerprint != media_file.fs_fingerprint:
        return CandidateRow(
            media_file_id=media_file_id,
            path=media_file.path,
            state=CandidateState.NEEDS_ANALYSIS,
            reason="Probe metadata is missing or stale.",
            eligible=True,
        )

    try:
        normalized_probe = parse_normalized_probe_json(probe.normalized_json)
    except ProbeError:
        return CandidateRow(
            media_file_id=media_file_id,
            path=media_file.path,
            state=CandidateState.BLOCKED,
            reason="Stored probe metadata could not be read.",
            eligible=False,
        )

    if not normalized_probe.video_streams:
        return CandidateRow(
            media_file_id=media_file_id,
            path=media_file.path,
            state=CandidateState.BLOCKED,
            reason="No video stream was found.",
            eligible=False,
        )

    primary_video = min(normalized_probe.video_streams, key=lambda stream: stream.index)
    codec = primary_video.codec.strip().lower() if primary_video.codec is not None else None
    if codec == "av1":
        return CandidateRow(
            media_file_id=media_file_id,
            path=media_file.path,
            state=CandidateState.ALREADY_SATISFIED,
            reason="Primary video is already AV1.",
            eligible=False,
        )

    return CandidateRow(
        media_file_id=media_file_id,
        path=media_file.path,
        state=CandidateState.READY,
        reason=f"Video is {codec.upper()}." if codec else "Video codec is available.",
        eligible=True,
    )


def _media_file_status_value(status: MediaFileStatus | str) -> str:
    if isinstance(status, MediaFileStatus):
        return status.value
    return status
