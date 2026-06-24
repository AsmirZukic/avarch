from __future__ import annotations

from datetime import datetime

from sqlmodel import Session

from avarch.adapters.sqlite.models import SchedulerState
from avarch.domain.scheduler import SchedulerMode


def get_or_create_scheduler_state(session: Session, *, now: datetime) -> SchedulerState:
    state = session.get(SchedulerState, 1)
    if state is None:
        state = SchedulerState(id=1, mode=SchedulerMode.RUNNING, updated_at=now)
        session.add(state)
        session.flush()
    return state