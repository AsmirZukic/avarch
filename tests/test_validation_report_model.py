from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from avarch.models.validation import (
    ValidationCheck,
    ValidationCheckStatus,
    ValidationReport,
)


def test_validation_report_passes_when_all_checks_pass(tmp_path: Path) -> None:
    report = _report(
        tmp_path,
        checks=[
            ValidationCheck(
                name="container_readable",
                status=ValidationCheckStatus.PASS,
                required=True,
            ),
            ValidationCheck(
                name="has_video_stream",
                status=ValidationCheckStatus.PASS,
                required=True,
            ),
        ],
    )

    assert report.passed is True


def test_validation_report_fails_when_any_required_check_fails(tmp_path: Path) -> None:
    report = _report(
        tmp_path,
        checks=[
            ValidationCheck(
                name="container_readable",
                status=ValidationCheckStatus.PASS,
                required=True,
            ),
            ValidationCheck(
                name="decode_health",
                status=ValidationCheckStatus.FAIL,
                required=True,
            ),
        ],
    )

    assert report.passed is False


def _report(tmp_path: Path, *, checks: list[ValidationCheck]) -> ValidationReport:
    now = datetime.now(UTC)
    return ValidationReport(
        plan_hash="plan-hash",
        policy_hash="policy-hash",
        source_path=tmp_path / "movie.mkv",
        output_path=tmp_path / "movie.av1.mkv",
        output_fs_fingerprint_after="output-after",
        passed=all(
            check.status == ValidationCheckStatus.PASS for check in checks if check.required
        ),
        checks=checks,
        warnings=[],
        observed=None,
        started_at=now,
        finished_at=now,
    )
