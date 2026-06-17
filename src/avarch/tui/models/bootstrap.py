from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from avarch.tui.models.common import TuiError


class BootstrapState(StrEnum):
    READY = "ready"
    CONFIGURATION_ERROR = "configuration_error"
    DATABASE_MISSING = "database_missing"
    DATABASE_UNINITIALIZED = "database_uninitialized"
    SCHEMA_ERROR = "schema_error"
    PROFILE_REGISTRY_ERROR = "profile_registry_error"


@dataclass(frozen=True, slots=True)
class BootstrapStatus:
    state: BootstrapState
    config_path: Path
    data_dir: Path
    database_url: str
    error: TuiError | None = None
