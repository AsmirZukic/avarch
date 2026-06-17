from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True, order=True)
class UiRevision:
    scheduler_generation: int
    newest_job_updated_at: datetime | None
    newest_attempt_updated_at: datetime | None
    newest_validation_created_at: datetime | None
    newest_promotion_updated_at: datetime | None


@dataclass(frozen=True, slots=True)
class TuiRecoveryAction:
    label: str
    action_id: str


@dataclass(frozen=True, slots=True)
class TuiError:
    title: str
    summary: str
    details: str | None = None
    recovery_actions: tuple[TuiRecoveryAction, ...] = ()
