from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from avarch.models.validation import (
    ValidationCheck,
    ValidationCheckStatus,
    ValidationReport,
    ValidationResult,
)


def test_validation_report_passes_when_all_checks_pass(tmp_path: Path) -> None:
    report = _report(
        tmp_path,
        checks=[
            ValidationCheck(name="container_readable", status=ValidationCheckStatus.PASS, required=True),
            ValidationCheck(name="has_video_stream", status=ValidationCheckStatus.PASS, required=True),
        ],
    )

    assert report.result == ValidationResult.PASS
    assert report.failure_reasons == []


def test_validation_report_fails_when_any_required_check_fails(tmp_path: Path) -> None:
    report = _report(
        tmp_path,
        checks=[
            ValidationCheck(name="container_readable", status=ValidationCheckStatus.PASS, required=True),
            ValidationCheck(name="decode_health", status=ValidationCheckStatus.FAIL, required=True),
        ],
    )

    assert report.result == ValidationResult.FAIL


def test_validation_report_preserves_failure_reasons(tmp_path: Path) -> None:
    report = _report(
        tmp_path,
        checks=[
            ValidationCheck(
                name="duration_close",
                status=ValidationCheckStatus.FAIL,
                required=True,
                message="duration differed by 8.2 seconds",
            ),
            ValidationCheck(
                name="codec_expected",
                status=ValidationCheckStatus.FAIL,
                required=True,
                message="expected av1, got h264",
            ),
        ],
    )

    assert report.failure_reasons == [
        "duration differed by 8.2 seconds",
        "expected av1, got h264",
    ]


def _report(tmp_path: Path, *, checks: list[ValidationCheck]) -> ValidationReport:
    now = datetime.now(UTC)
    return ValidationReport(
        plan_hash="plan-hash",
        policy_hash="policy-hash",
        source_path=tmp_path / "movie.mkv",
        output_path=tmp_path / "movie.av1.mkv",
        source_fs_fingerprint_before="source-before",
        source_fs_fingerprint_after="source-after",
        output_fs_fingerprint_before="output-before",
        output_fs_fingerprint_after="output-after",
        passed=all(check.status == ValidationCheckStatus.PASS for check in checks if check.required),
        checks=checks,
        warnings=[],
        observed=None,
        started_at=now,
        finished_at=now,
    )