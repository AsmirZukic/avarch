from pathlib import Path

import pytest
from pydantic import ValidationError

from avarch.models.plan import (
    AudioPlan,
    Av1anCommandSpec,
    PlanArtifactPaths,
    SubtitlePlan,
    SubtitleStreamPlan,
    TranscodePlan,
    ValidationPolicy,
    VideoPlan,
)


def test_plan_serializes_paths_as_strings() -> None:
    plan = _plan()

    data = plan.model_dump(mode="json")

    assert data["input_path"] == "/media/movie.mkv"
    assert data["artifacts"]["plan_json"] == "/work/plan.json"


def test_plan_round_trips_through_json() -> None:
    plan = _plan()

    restored = TranscodePlan.model_validate_json(plan.model_dump_json())

    assert restored == plan


def test_plan_defaults_to_manual_review() -> None:
    data = _plan().model_dump()
    data.pop("promotion")

    plan = TranscodePlan.model_validate(data)

    assert plan.promotion.mode == "manual_review"


def test_av1an_spec_defaults_to_resume() -> None:
    spec = Av1anCommandSpec(
        input_path=Path("/media/movie.mkv"),
        output_path=Path("/output/movie.mkv"),
        temp_dir=Path("/work/temp"),
        encoder="svt-av1",
        encoder_args=["--crf", "28"],
        workers=6,
    )

    assert spec.resume is True


def test_plan_requires_valid_stream_indexes() -> None:
    with pytest.raises(ValidationError):
        VideoPlan(
            source_stream_index=-1,
            source_codec="hevc",
            source_width=3840,
            source_height=2160,
            source_bit_depth=10,
            source_hdr_metadata_present=True,
            max_width=1920,
            resize_required=True,
            hdr_to_sdr=True,
            source="vapoursynth",
        )


def _plan() -> TranscodePlan:
    input_path = Path("/media/movie.mkv")
    output_path = Path("/output/movie.mkv")
    temp_dir = Path("/work/temp")
    return TranscodePlan(
        plan_hash="plan-hash",
        input_path=input_path,
        output_path=output_path,
        temp_dir=temp_dir,
        media_file_id=1,
        source_fs_fingerprint="fs-v1",
        profile_name="av1_1080p_sdr",
        profile_hash="profile-hash",
        probe_hash="probe-hash",
        video=VideoPlan(
            source_stream_index=0,
            source_codec="hevc",
            source_width=3840,
            source_height=2160,
            source_bit_depth=10,
            source_hdr_metadata_present=True,
            max_width=1920,
            resize_required=True,
            hdr_to_sdr=True,
            source="vapoursynth",
        ),
        audio=AudioPlan(
            source_stream_index=1,
            source_codec="aac",
            source_language="eng",
            source_channels=6,
            source_title="Main",
            source_commentary=False,
            target_codec="libopus",
            target_bitrate="128k",
            target_channels=2,
        ),
        subtitles=SubtitlePlan(
            streams=[
                SubtitleStreamPlan(
                    source_stream_index=2,
                    codec="subrip",
                    language="eng",
                    title="English",
                    forced=False,
                )
            ]
        ),
        av1an=Av1anCommandSpec(
            input_path=input_path,
            output_path=output_path,
            temp_dir=temp_dir,
            encoder="svt-av1",
            encoder_args=["--preset", "6", "--crf", "28"],
            workers=6,
        ),
        validation=ValidationPolicy(
            expected_container="mkv",
            maximum_width=1920,
            expected_audio_codec="libopus",
            expected_audio_channels=2,
            expected_subtitle_streams=[2],
            source_duration_seconds=600.0,
        ),
        artifacts=PlanArtifactPaths(
            artifact_dir=Path("/work"),
            plan_json=Path("/work/plan.json"),
            vapoursynth_script=Path("/work/movie.vpy"),
            av1an_command_json=Path("/work/av1an.command.json"),
            validation_policy_json=Path("/work/validation-policy.json"),
        ),
    )
