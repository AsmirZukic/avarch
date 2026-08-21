from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Protocol


class ValidationCheckStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    WARNING = "warning"
    SKIPPED = "skipped"


class ValidationCheckLike(Protocol):
    @property
    def status(self) -> ValidationCheckStatus: ...

    @property
    def required(self) -> bool: ...


def checks_pass(checks: Iterable[ValidationCheckLike]) -> bool:
    return all(check.status == ValidationCheckStatus.PASS for check in checks if check.required)
