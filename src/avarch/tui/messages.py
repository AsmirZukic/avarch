from __future__ import annotations

from dataclasses import dataclass

from avarch.tui.models.common import TuiError, UiRevision
from avarch.tui.state import TuiRoute


@dataclass(frozen=True, slots=True)
class RefreshStarted:
    route: TuiRoute
    request_id: int


@dataclass(frozen=True, slots=True)
class RefreshCompleted:
    route: TuiRoute
    request_id: int
    revision: UiRevision
    rendered: bool


@dataclass(frozen=True, slots=True)
class RefreshFailed:
    route: TuiRoute
    request_id: int
    error: TuiError
