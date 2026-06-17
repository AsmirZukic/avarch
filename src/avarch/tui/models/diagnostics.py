from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    name: str
    status: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class DoctorSnapshot:
    checks: tuple[DoctorCheck, ...]
