from __future__ import annotations

from textual.app import ComposeResult
from textual.widgets import Static

from avarch.tui.models.jobs import ValidationCheckSnapshot, ValidationSnapshot


class ValidationChecksView(Static):
    def __init__(self, validation: ValidationSnapshot | None, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.validation = validation
        self.content_text = validation_review_text(validation)

    def compose(self) -> ComposeResult:
        yield Static(self.content_text, id="validation-checks-content")

    def update_validation(self, validation: ValidationSnapshot | None) -> None:
        self.validation = validation
        self.content_text = validation_review_text(validation)
        if self.is_mounted:
            self.query_one("#validation-checks-content", Static).update(self.content_text)


def validation_review_text(validation: ValidationSnapshot | None) -> str:
    if validation is None:
        return "Validation\n\nNo validation result is available."
    result = "passed" if validation.passed else "failed"
    lines = [
        "Validation",
        "",
        f"Result: {result}",
        f"Validation result: {validation.validation_id}",
        f"Policy hash: {validation.policy_hash}",
        f"Output: {validation.output_path}",
    ]
    if validation.warnings:
        lines.extend(["", "Warnings"])
        lines.extend(f"- {warning}" for warning in validation.warnings)
    lines.extend(["", "Checks"])
    if not validation.checks:
        lines.append("No structured checks were recorded.")
    for check in ordered_validation_checks(validation.checks):
        lines.extend(_check_lines(check))
    return "\n".join(lines)


def ordered_validation_checks(
    checks: tuple[ValidationCheckSnapshot, ...],
) -> tuple[ValidationCheckSnapshot, ...]:
    return tuple(sorted(checks, key=_check_order_key))


def _check_order_key(check: ValidationCheckSnapshot) -> tuple[int, str]:
    if check.required and check.status in {"fail", "skipped"}:
        bucket = 0
    elif check.status == "warning":
        bucket = 1
    elif check.status == "skipped":
        bucket = 2
    elif check.status == "fail":
        bucket = 3
    else:
        bucket = 4
    return bucket, check.name


def _check_lines(check: ValidationCheckSnapshot) -> list[str]:
    requirement = "required" if check.required else "optional"
    status = "FAIL" if check.required and check.status == "skipped" else check.status.upper()
    lines = [f"- {status} {requirement} {check.name}"]
    if check.expected is not None:
        lines.append(f"  Expected: {check.expected}")
    if check.observed is not None:
        lines.append(f"  Observed: {check.observed}")
    if check.message is not None:
        lines.append(f"  Message: {check.message}")
    return lines
