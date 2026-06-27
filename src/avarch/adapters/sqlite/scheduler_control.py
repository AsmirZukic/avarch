from __future__ import annotations

from datetime import datetime

from sqlmodel import Session

from avarch.adapters.sqlite import scheduler_state
from avarch.application.scheduler_control import SchedulerControlWorkflowError


class SqliteSchedulerControlStore:
    def __init__(self, session: Session) -> None:
        self._session = session

    def pause(self, *, now: datetime, reason: str | None) -> None:
        try:
            scheduler_state.pause_scheduler(self._session, now=now, reason=reason)
        except scheduler_state.SchedulerControlError as exc:
            raise SchedulerControlWorkflowError(str(exc)) from exc

    def resume(self, *, now: datetime) -> None:
        try:
            scheduler_state.resume_scheduler(self._session, now=now)
        except scheduler_state.SchedulerControlError as exc:
            raise SchedulerControlWorkflowError(str(exc)) from exc

    def drain(self, *, now: datetime, reason: str | None) -> None:
        try:
            scheduler_state.drain_scheduler(self._session, now=now, reason=reason)
        except scheduler_state.SchedulerControlError as exc:
            raise SchedulerControlWorkflowError(str(exc)) from exc

    def stop(self, *, now: datetime, reason: str | None) -> None:
        try:
            scheduler_state.stop_scheduler(self._session, now=now, reason=reason)
        except scheduler_state.SchedulerControlError as exc:
            raise SchedulerControlWorkflowError(str(exc)) from exc
