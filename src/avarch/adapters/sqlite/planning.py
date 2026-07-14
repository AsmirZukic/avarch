from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from types import TracebackType

from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from avarch.adapters.filesystem.workspace import WorkspaceContext, WorkspaceError
from avarch.adapters.sqlite.inventory import select_inventory_files
from avarch.adapters.sqlite.models import MediaFile, MediaFileStatus, MediaPlan, ProbeResult
from avarch.application.planning import (
    PlanningContext,
    PlanningError,
    PlanningInputFile,
    PlanningInputSelection,
    PlanningProfile,
)
from avarch.application.probe_summary import ProbeSummaryError, parse_normalized_probe_json
from avarch.models.plan import TranscodePlan


class SqlitePlanningStore:
    def __init__(self, session: Session) -> None:
        self._session = session

    def select_inputs(
        self,
        *,
        file_selectors: Sequence[Path],
        workspace_root: Path,
        resolve_path: Callable[[Path], Path],
    ) -> PlanningInputSelection:
        selection = select_inventory_files(
            self._session,
            file_selectors=file_selectors,
            workspace_root=workspace_root,
            resolve_path=resolve_path,
        )
        return PlanningInputSelection(
            selected=tuple(
                _planning_input(self._session, media_file) for media_file in selection.selected
            ),
            missing=selection.missing,
        )

    def eligible_inputs(self) -> list[PlanningInputFile]:
        return [
            _planning_input(self._session, media_file)
            for media_file in eligible_files_for_planning(self._session)
        ]

    def load_context(
        self,
        *,
        input_path: Path,
        resolved_profile: PlanningProfile,
    ) -> PlanningContext:
        return load_planning_context(
            self._session,
            input_path=input_path,
            resolved_profile=resolved_profile,
        )

    def equivalent_current_plan_exists(
        self,
        *,
        media_file_id: int | None,
        probe_hash: str,
        source_fs_fingerprint: str,
        profile_hash: str,
        execution_identity_hash: str,
    ) -> bool:
        return equivalent_current_plan_exists(
            self._session,
            media_file_id=media_file_id,
            probe_hash=probe_hash,
            source_fs_fingerprint=source_fs_fingerprint,
            profile_hash=profile_hash,
            execution_identity_hash=execution_identity_hash,
        )

    def persist_plan(self, *, plan: TranscodePlan, now: datetime) -> None:
        persist_media_plan(self._session, plan=plan, now=now)


class SqlitePlanningUnitOfWork:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._session: Session | None = None

    def __enter__(self) -> SqlitePlanningStore:
        session = Session(self._engine)
        self._session = session
        return SqlitePlanningStore(session)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc, traceback
        if self._session is None:
            return
        try:
            if exc_type is None:
                self._session.commit()
            else:
                self._session.rollback()
        finally:
            self._session.close()
            self._session = None


def load_planning_context(
    session: Session,
    *,
    input_path: Path,
    resolved_profile: PlanningProfile,
) -> PlanningContext:
    media_file = _get_media_file_for_input(session, input_path)
    if media_file is None:
        raise PlanningError("File is not present in the media inventory.")
    if _status_value(media_file.status) == MediaFileStatus.MISSING.value:
        raise PlanningError("File is marked missing in the media inventory.")
    if media_file.latest_probe_id is None:
        raise PlanningError("No canonical probe result exists for this file.")

    probe_result = session.get(ProbeResult, media_file.latest_probe_id)
    if probe_result is None:
        raise PlanningError("The canonical probe result no longer exists.")
    if probe_result.media_file_id != media_file.id:
        raise PlanningError("The canonical probe belongs to another media file.")
    if probe_result.source_fs_fingerprint != media_file.fs_fingerprint:
        raise PlanningError(
            "The canonical probe does not match the current filesystem fingerprint.\n"
            "Run avarch probe for this file again."
        )

    try:
        normalized_probe = parse_normalized_probe_json(probe_result.normalized_json)
    except ProbeSummaryError as exc:
        raise PlanningError(str(exc)) from exc
    if not normalized_probe.video_streams:
        raise PlanningError("The canonical probe contains no video stream.")

    return PlanningContext(
        media_file=media_file,
        probe_result=probe_result,
        normalized_probe=normalized_probe,
        profile_name=resolved_profile.name,
        profile=resolved_profile.profile,
        relative_path_root=_relative_path_root(input_path),
    )


def eligible_files_for_planning(session: Session) -> list[MediaFile]:
    media_files = list(
        session.exec(
            select(MediaFile)
            .where(MediaFile.status != MediaFileStatus.MISSING)
            .order_by(MediaFile.path)
        ).all()
    )
    eligible: list[MediaFile] = []
    for media_file in media_files:
        if media_file.latest_probe_id is None:
            continue
        probe_result = session.get(ProbeResult, media_file.latest_probe_id)
        if probe_result is None:
            continue
        if probe_result.source_fs_fingerprint != media_file.fs_fingerprint:
            continue
        eligible.append(media_file)
    return eligible


def equivalent_current_plan_exists(
    session: Session,
    *,
    media_file_id: int | None,
    probe_hash: str,
    source_fs_fingerprint: str,
    profile_hash: str,
    execution_identity_hash: str,
) -> bool:
    if media_file_id is None:
        return False
    return (
        session.exec(
            select(MediaPlan).where(
                MediaPlan.media_file_id == media_file_id,
                MediaPlan.probe_hash == probe_hash,
                MediaPlan.source_fs_fingerprint == source_fs_fingerprint,
                MediaPlan.profile_hash == profile_hash,
                MediaPlan.execution_identity_hash == execution_identity_hash,
                MediaPlan.is_current == True,  # noqa: E712
                MediaPlan.is_valid == True,  # noqa: E712
            )
        ).first()
        is not None
    )


def persist_media_plan(session: Session, *, plan: TranscodePlan, now: datetime) -> MediaPlan:
    existing = session.exec(select(MediaPlan).where(MediaPlan.plan_hash == plan.plan_hash)).first()
    if existing is not None:
        existing.is_current = True
        existing.is_valid = True
        existing.superseded_at = None
        session.add(existing)
        _supersede_other_current_plans(session, plan=plan, keep_plan_id=existing.id, now=now)
        return existing

    probe_result = session.exec(
        select(ProbeResult).where(
            ProbeResult.media_file_id == plan.media_file_id,
            ProbeResult.probe_hash == plan.probe_hash,
        )
    ).first()
    if probe_result is None or probe_result.id is None:
        raise PlanningError("The planned probe result no longer exists.")

    media_plan = MediaPlan(
        media_file_id=plan.media_file_id,
        probe_result_id=probe_result.id,
        profile_name=plan.profile_name,
        profile_hash=plan.profile_hash,
        probe_hash=plan.probe_hash,
        source_fs_fingerprint=plan.source_fs_fingerprint,
        execution_identity_hash=plan.execution_identity.identity_hash,
        plan_hash=plan.plan_hash,
        plan_path=str(plan.artifacts.plan_json),
        output_path=str(plan.output_path),
        is_current=True,
        is_valid=True,
        created_at=now,
    )
    session.add(media_plan)
    session.flush()
    _supersede_other_current_plans(session, plan=plan, keep_plan_id=media_plan.id, now=now)
    return media_plan


def current_plan_for_file(session: Session, media_file: MediaFile) -> MediaPlan | None:
    if media_file.id is None:
        return None
    return session.exec(
        select(MediaPlan)
        .where(
            MediaPlan.media_file_id == media_file.id,
            MediaPlan.is_current == True,  # noqa: E712
            MediaPlan.is_valid == True,  # noqa: E712
        )
        .order_by(col(MediaPlan.created_at).desc(), col(MediaPlan.id).desc())
    ).first()


def find_plan(session: Session, selector: str) -> MediaPlan | None:
    if selector.isdecimal():
        plan = session.get(MediaPlan, int(selector))
        if plan is not None:
            return plan
    return session.exec(select(MediaPlan).where(MediaPlan.plan_hash == selector)).first()


def _get_media_file_for_input(session: Session, input_path: Path) -> MediaFile | None:
    resolved = input_path.resolve()
    candidates = [str(resolved)]
    try:
        workspace = WorkspaceContext.discover(resolved.parent)
        candidates.append(str(resolved.relative_to(workspace.root)))
    except (WorkspaceError, ValueError):
        pass

    for candidate in dict.fromkeys(candidates):
        media_file = session.exec(select(MediaFile).where(MediaFile.path == candidate)).first()
        if media_file is not None:
            return media_file
    return None


def _relative_path_root(input_path: Path) -> Path | None:
    try:
        return WorkspaceContext.discover(input_path.resolve().parent).root
    except WorkspaceError:
        return None


def _status_value(status: MediaFileStatus | str) -> str:
    if isinstance(status, MediaFileStatus):
        return status.value
    return status


def _planning_input(session: Session, media_file: MediaFile) -> PlanningInputFile:
    return PlanningInputFile(
        id=media_file.id,
        path=media_file.path,
        size_bytes=media_file.size_bytes,
        fs_fingerprint=media_file.fs_fingerprint,
        probe_state=_probe_state(session, media_file),
    )


def _probe_state(session: Session, media_file: MediaFile) -> str:
    if media_file.latest_probe_id is None:
        return "missing"
    probe_result = session.get(ProbeResult, media_file.latest_probe_id)
    if probe_result is None or probe_result.source_fs_fingerprint != media_file.fs_fingerprint:
        return "stale"
    return "current"


def _supersede_other_current_plans(
    session: Session,
    *,
    plan: TranscodePlan,
    keep_plan_id: int | None,
    now: datetime,
) -> None:
    plans = list(
        session.exec(
            select(MediaPlan).where(
                MediaPlan.media_file_id == plan.media_file_id,
                MediaPlan.profile_name == plan.profile_name,
                MediaPlan.is_current == True,  # noqa: E712
            )
        ).all()
    )
    for media_plan in plans:
        if media_plan.id == keep_plan_id:
            continue
        media_plan.is_current = False
        media_plan.superseded_at = now
        session.add(media_plan)
