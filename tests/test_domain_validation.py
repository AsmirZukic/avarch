from __future__ import annotations

from dataclasses import dataclass

from avarch.domain.validation import (
    ValidationCheckStatus,
    checks_pass,
)


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: ValidationCheckStatus
    required: bool
    message: str | None = None


def test_checks_pass_when_required_checks_pass() -> None:
    assert checks_pass(
        [
            Check("required", ValidationCheckStatus.PASS, required=True),
            Check("optional", ValidationCheckStatus.FAIL, required=False),
        ]
    )


def test_checks_fail_when_required_check_fails() -> None:
    assert not checks_pass([Check("required", ValidationCheckStatus.FAIL, required=True)])
