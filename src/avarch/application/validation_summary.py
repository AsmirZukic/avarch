from __future__ import annotations

from avarch.domain.validation import ValidationCheckStatus
from avarch.models.validation import ValidationReport


def failed_required_check_names(report: ValidationReport) -> list[str]:
    return [
        check.name
        for check in report.checks
        if check.required and check.status != ValidationCheckStatus.PASS
    ]


def failed_check_summary(report: ValidationReport) -> str:
    names = failed_required_check_names(report)
    if not names:
        return "Validation failed."
    return "Validation failed required checks: " + ", ".join(names)


def format_validation_report_summary(
    report: ValidationReport,
    *,
    reused: bool = False,
) -> str:
    lines = ["Validation PASS" if report.passed else "Validation FAIL", ""]
    if reused:
        lines.append("Existing passing validation reused.")
        lines.append("")
    lines.extend(
        [
            f"Output:       {report.output_path}",
            f"Source:       {report.source_path}",
            f"Plan hash:    {report.plan_hash}",
            f"Policy hash:  {report.policy_hash}",
            "",
        ]
    )
    for check in report.checks:
        lines.append(f"{check.status.value.upper():<5} {check.name}")
    return "\n".join(lines)
