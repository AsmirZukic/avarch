from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from avarch.tui.models.common import UiRevision
from avarch.tui.models.jobs import JobAttemptSnapshot, JobDetailSnapshot, JobPlanSnapshot
from avarch.tui.screens.job_detail import JobDetailView


def test_job_detail_shows_current_state() -> None:
    async def run() -> None:
        view = JobDetailView(_snapshot(status="RUNNING", stage="encode", priority=5))

        assert "Status: RUNNING" in view.content_text
        assert "Stage: encode" in view.content_text
        assert "Priority: 5" in view.content_text
        assert "Movie.mkv" in view.content_text

    asyncio.run(run())


def test_job_detail_shows_profile_identity() -> None:
    async def run() -> None:
        view = JobDetailView(_snapshot(profile_name="film_grain", profile_hash="profile-hash"))

        assert "Profile: film_grain" in view.content_text
        assert "Profile hash: profile-hash" in view.content_text

    asyncio.run(run())


def test_job_detail_shows_control_request() -> None:
    async def run() -> None:
        view = JobDetailView(
            _snapshot(
                control_request="Cancel requested by tui",
                control_reason="bad source",
            )
        )

        assert "Control: Cancel requested by tui" in view.content_text
        assert "Reason: bad source" in view.content_text

    asyncio.run(run())


def test_plan_tab_shows_selected_streams() -> None:
    async def run() -> None:
        view = JobDetailView(_snapshot(plan=_plan()))
        view.select_tab("plan")

        assert "Video stream: 0" in view.content_text
        assert "Audio stream: 1" in view.content_text
        assert "Subtitle streams: 2, 3" in view.content_text

    asyncio.run(run())


def test_plan_tab_shows_vpy_mode() -> None:
    async def run() -> None:
        view = JobDetailView(_snapshot(plan=_plan(vapoursynth_mode="custom_template")))
        view.select_tab("plan")

        assert "VapourSynth mode: custom_template" in view.content_text
        assert "VapourSynth identity: vpy-id" in view.content_text

    asyncio.run(run())


def test_plan_tab_shows_validation_policy() -> None:
    async def run() -> None:
        view = JobDetailView(_snapshot(plan=_plan(validation_policy_hash="policy-hash")))
        view.select_tab("plan")

        assert "Validation policy: policy-hash" in view.content_text
        assert "Expected video: av1 1920x1080" in view.content_text
        assert "Expected audio: opus 2ch" in view.content_text
        assert "Decode sample: enabled" in view.content_text

    asyncio.run(run())


def test_plan_artifact_view_is_read_only() -> None:
    async def run() -> None:
        view = JobDetailView(_snapshot(plan=_plan(artifact_text='{"plan_hash":"plan-hash"}')))
        view.select_tab("artifact")

        assert view.plan_artifact_editable is False
        assert "Read-only plan artifact" in view.content_text
        assert '{"plan_hash":"plan-hash"}' in view.content_text

    asyncio.run(run())


def test_attempts_are_ordered() -> None:
    async def run() -> None:
        view = JobDetailView(_snapshot(attempts=(_attempt(2), _attempt(1), _attempt(3))))
        view.select_tab("attempts")

        text = view.content_text
        assert text.index("Attempt 1") < text.index("Attempt 2") < text.index("Attempt 3")

    asyncio.run(run())


def test_attempt_selection_updates_details() -> None:
    async def run() -> None:
        view = JobDetailView(
            _snapshot(
                attempts=(
                    _attempt(1, stage="probe", status="completed"),
                    _attempt(2, stage="encode", status="running", runner_id="runner-2"),
                )
            )
        )

        view.select_attempt(2)

        assert view.selected_attempt_number == 2
        assert "Selected attempt: 2" in view.content_text
        assert "Stage: encode" in view.content_text
        assert "Runner: runner-2" in view.content_text

    asyncio.run(run())


def _snapshot(
    *,
    status: str = "PENDING",
    stage: str = "probe",
    priority: int = 0,
    profile_name: str = "av1_1080p",
    profile_hash: str = "hash",
    control_request: str | None = None,
    control_reason: str | None = None,
    plan: JobPlanSnapshot | None = None,
    attempts: tuple[JobAttemptSnapshot, ...] = (),
) -> JobDetailSnapshot:
    now = datetime.now(UTC)
    return JobDetailSnapshot(
        revision=UiRevision(
            scheduler_generation=1,
            newest_job_updated_at=now,
            newest_attempt_updated_at=None,
            newest_validation_created_at=None,
            newest_promotion_updated_at=None,
        ),
        job_id=7,
        media_file_id=3,
        file_name="Movie.mkv",
        source_path="/media/Movie.mkv",
        status=status,
        stage=stage,
        profile_name=profile_name,
        profile_hash=profile_hash,
        priority=priority,
        attempts_count=len(attempts),
        queue_key="queue-key",
        source_fs_fingerprint="source-fingerprint",
        probe_hash="probe-hash",
        plan_hash=plan.plan_hash if plan is not None else None,
        plan_path=plan.plan_path if plan is not None else None,
        output_path="/media/Movie.av1.mkv",
        last_error_type=None,
        last_error_message=None,
        control_request=control_request,
        control_reason=control_reason,
        plan=plan,
        created_at=now,
        updated_at=now,
        started_at=None,
        finished_at=None,
        attempts=attempts,
        events=(),
        latest_validation=None,
        latest_promotion=None,
    )


def _attempt(
    attempt_number: int,
    *,
    stage: str = "encode",
    status: str = "completed",
    runner_id: str = "runner",
) -> JobAttemptSnapshot:
    now = datetime.now(UTC)
    return JobAttemptSnapshot(
        attempt_id=attempt_number,
        attempt_number=attempt_number,
        stage=stage,
        resource_class="cheap",
        status=status,
        runner_id=runner_id,
        started_at=now,
        finished_at=None if status == "running" else now,
        stdout_log=f"/logs/{attempt_number}.stdout.log",
        stderr_log=f"/logs/{attempt_number}.stderr.log",
        output_path=None,
        error_type=None,
        error_message=None,
    )


def _plan(
    *,
    vapoursynth_mode: str = "generated",
    validation_policy_hash: str = "validation-hash",
    artifact_text: str = "{}",
) -> JobPlanSnapshot:
    return JobPlanSnapshot(
        plan_hash="plan-hash",
        plan_path="/plans/plan.json",
        artifact_text=artifact_text,
        artifact_read_only=True,
        video_stream_index=0,
        audio_stream_index=1,
        subtitle_stream_indexes=(2, 3),
        vapoursynth_mode=vapoursynth_mode,
        vapoursynth_identity_hash="vpy-id",
        vapoursynth_template_path="/profiles/custom.vpy",
        vapoursynth_template_hash="template-hash",
        validation_policy_hash=validation_policy_hash,
        validation_expected_video_codec="av1",
        validation_expected_width=1920,
        validation_expected_height=1080,
        validation_expected_audio_codec="opus",
        validation_expected_audio_channels=2,
        validation_expected_audio_language="eng",
        validation_decode_sample=True,
    )
