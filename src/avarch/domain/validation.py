from __future__ import annotations

from collections.abc import Iterable
from enum import StrEnum
from typing import Protocol


class ValidationCheckStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    WARNING = "warning"
    SKIPPED = "skipped"


class ValidationResult(StrEnum):
    PASS = "pass"
    FAIL = "fail"


class ValidationCheckLike(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def status(self) -> ValidationCheckStatus: ...

    @property
    def required(self) -> bool: ...

    @property
    def message(self) -> str | None: ...


def checks_pass(checks: Iterable[ValidationCheckLike]) -> bool:
    return all(check.status == ValidationCheckStatus.PASS for check in checks if check.required)


def failure_reasons_for_checks(checks: Iterable[ValidationCheckLike]) -> list[str]:
    return [
        check.message or check.name
        for check in checks
        if check.required and check.status != ValidationCheckStatus.PASS
    ]