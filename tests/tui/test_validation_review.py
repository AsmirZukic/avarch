from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from avarch.tui.models.common import UiRevision
from avarch.tui.models.jobs import (
    JobDetailSnapshot,
    ValidationCheckSnapshot,
    ValidationSnapshot,
)
from avarch.tui.screens.job_detail import JobDetailView
from avarch.tui.widgets.validation_checks import ValidationChecksView


def test_validation_tab_shows_passed_checks() -> None:
    async def run() -> None:
        view = ValidationChecksView(
            _validation(passed=True, checks=(_check("container", "pass"),))
        )

        assert "Result: passed" in view.content_text
        assert "- PASS required container" in view.content_text

    asyncio.run(run())


def test_validation_tab_shows_failed_checks_first() -> None:
    async def run() -> None:
        view = ValidationChecksView(
            _validation(
                passed=False,
                checks=(
                    _check("video_codec", "pass"),
                    _check("duration", "fail", message="too short"),
                ),
            )
        )

        assert view.content_text.index("duration") < view.content_text.index("video_codec")

    asyncio.run(run())


def test_validation_tab_shows_expected_and_observed() -> None:
    async def run() -> None:
        view = ValidationChecksView(
            _validation(
                passed=False,
                checks=(
                    _check(
                        "audio_codec",
                        "fail",
                        expected="opus",
                        observed="aac",
                    ),
                ),
            )
        )

        assert "Expected: opus" in view.content_text
        assert "Observed: aac" in view.content_text

    asyncio.run(run())


def test_required_skip_is_shown_as_failure() -> None:
    async def run() -> None:
        view = ValidationChecksView(
            _validation(
                passed=False,
                checks=(_check("decode_sample", "skipped", required=True),),
            )
        )

        assert "- FAIL required decode_sample" in view.content_text

    asyncio.run(run())


def test_optional_skip_is_identified() -> None:
    async def run() -> None:
        view = ValidationChecksView(
            _validation(
                passed=True,
                checks=(_check("size_reduction", "skipped", required=False),),
            )
        )

        assert "- SKIPPED optional size_reduction" in view.content_text

    asyncio.run(run())


def test_failed_validation_offers_retry() -> None:
    async def run() -> None:
        view = JobDetailView(_snapshot(_validation(passed=False)))
        view.select_tab("validation")

        assert "Next action: retry workflow" in view.content_text

    asyncio.run(run())


def test_passing_validation_offers_promotion() -> None:
    async def run() -> None:
        view = JobDetailView(_snapshot(_validation(passed=True)))
        view.select_tab("validation")

        assert "Next action: review promotion" in view.content_text

    asyncio.run(run())


def _validation(
    *,
    passed: bool,
    checks: tuple[ValidationCheckSnapshot, ...] = (),
) -> ValidationSnapshot:
    return ValidationSnapshot(
        validation_id=11,
        passed=passed,
        output_path="/media/Movie.av1.mkv",
        plan_hash="plan-hash",
        policy_hash="policy-hash",
        created_at=datetime.now(UTC),
        checks=checks,
        warnings=(),
    )


def _check(
    name: str,
    status: str,
    *,
    required: bool = True,
    expected: str | None = None,
    observed: str | None = None,
    message: str | None = None,
) -> ValidationCheckSnapshot:
    return ValidationCheckSnapshot(
        name=name,
        status=status,
        required=required,
        expected=expected,
        observed=observed,
        message=message,
    )


def _snapshot(validation: ValidationSnapshot) -> JobDetailSnapshot:
    now = datetime.now(UTC)
    return JobDetailSnapshot(
        revision=UiRevision(
            scheduler_generation=1,
            newest_job_updated_at=now,
            newest_attempt_updated_at=None,
            newest_validation_created_at=now,
            newest_promotion_updated_at=None,
        ),
        job_id=7,
        media_file_id=3,
        file_name="Movie.mkv",
        source_path="/media/Movie.mkv",
        status="VALIDATED" if validation.passed else "FAILED",
        stage="promote" if validation.passed else "validate",
        profile_name="av1_1080p",
        profile_hash="profile-hash",
        priority=0,
        attempts_count=1,
        queue_key="queue-key",
        source_fs_fingerprint="source-fingerprint",
        probe_hash="probe-hash",
        plan_hash="plan-hash",
        plan_path="/plans/plan.json",
        output_path="/media/Movie.av1.mkv",
        last_error_type=None,
        last_error_message=None,
        control_request=None,
        control_reason=None,
        plan=None,
        created_at=now,
        updated_at=now,
        started_at=None,
        finished_at=None,
        attempts=(),
        events=(),
        latest_validation=validation,
        latest_promotion=None,
    )
