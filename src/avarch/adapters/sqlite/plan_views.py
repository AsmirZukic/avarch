from __future__ import annotations

from sqlmodel import Session, col, select

from avarch.adapters.sqlite.models import MediaFile, MediaPlan
from avarch.adapters.sqlite.planning import find_plan
from avarch.application.plan_views import PlanDetail, PlanListItem


class SqlitePlanViewStore:
    def __init__(self, session: Session) -> None:
        self._session = session

    def list_plans(self, *, current_only: bool) -> list[PlanListItem]:
        statement = select(MediaPlan).order_by(
            col(MediaPlan.created_at).asc(),
            col(MediaPlan.id).asc(),
        )
        if current_only:
            statement = statement.where(
                MediaPlan.is_current == True,  # noqa: E712
                MediaPlan.is_valid == True,  # noqa: E712
            )
        rows = list(self._session.exec(statement).all())
        media_by_id = {
            media_file.id: media_file
            for media_file in self._session.exec(select(MediaFile)).all()
            if media_file.id is not None
        }
        return [
            PlanListItem(
                id=row.id,
                media_file_id=row.media_file_id,
                media_path=media_by_id[row.media_file_id].path
                if row.media_file_id in media_by_id
                else None,
                profile_name=row.profile_name,
                plan_hash=row.plan_hash,
                current=row.is_current and row.is_valid,
            )
            for row in rows
        ]

    def plan_detail(self, *, selector: str) -> PlanDetail | None:
        plan = find_plan(self._session, selector)
        if plan is None:
            return None
        media_file = self._session.get(MediaFile, plan.media_file_id)
        return PlanDetail(
            id=plan.id,
            media_path=media_file.path if media_file is not None else None,
            profile_name=plan.profile_name,
            current=plan.is_current and plan.is_valid,
            plan_hash=plan.plan_hash,
            probe_hash=plan.probe_hash,
            output_path=plan.output_path,
            plan_path=plan.plan_path,
        )
