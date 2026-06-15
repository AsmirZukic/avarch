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
    VapourSynthPlan,
    VideoPlan,
)


def test_plan_serializes_paths_as_strings() -> None:
    plan = sample_plan()

    data = plan.model_dump(mode="json")

    assert data["input_path"] == "/media/movie.mkv"
    assert data["artifacts"]["plan_json"] == "/work/plan.json"


def test_plan_round_trips_through_json() -> None:
    plan = sample_plan()

    restored = TranscodePlan.model_validate_json(plan.model_dump_json())

    assert restored == plan


def test_plan_defaults_to_manual_review() -> None:
    data = sample_plan().model_dump()
    data.pop("promotion")

    plan = TranscodePlan.model_validate(data)

    assert plan.promotion.mode == "manual_review"


def test_transcode_plan_schema_version_is_two() -> None:
    assert sample_plan().schema_version == 2


def test_transcode_plan_contains_vapoursynth_spec() -> None:
    plan = sample_plan()

    assert plan.vapoursynth.mode == "generated"
    assert plan.vapoursynth.output_format == "YUV420P10"
    assert plan.vapoursynth.resize_filter == "spline36"


def test_av1an_input_matches_vapoursynth_script_path() -> None:
    data = sample_plan().model_dump()
    data["av1an"]["input_path"] = Path("/other/movie.mkv")

    with pytest.raises(ValidationError):
        TranscodePlan.model_validate(data)


def test_video_plan_contains_source_pixel_and_color_metadata() -> None:
    plan = sample_plan()

    assert plan.video.source_pix_fmt == "yuv420p10le"
    assert plan.video.source_color_transfer == "bt709"
    assert plan.video.source_color_primaries == "bt709"
    assert plan.video.source_color_space == "bt709"


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
            source_pix_fmt="yuv420p10le",
            source_color_transfer="bt709",
            source_color_primaries="bt709",
            source_color_space="bt709",
            source_hdr_metadata_present=True,
            max_width=1920,
            target_width=1920,
            target_height=1080,
            resize_required=True,
            hdr_to_sdr=True,
            source="vapoursynth",
        )


def sample_plan() -> TranscodePlan:
    input_path = Path("/media/movie.mkv")
    output_path = Path("/output/movie.mkv")
    temp_dir = Path("/work/temp")
    script_path = Path("/work/movie.vpy")
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
            source_pix_fmt="yuv420p10le",
            source_color_transfer="bt709",
            source_color_primaries="bt709",
            source_color_space="bt709",
            source_hdr_metadata_present=False,
            max_width=1920,
            target_width=1920,
            target_height=1080,
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
        vapoursynth=VapourSynthPlan(
            mode="generated",
            script_path=script_path,
            source_path=input_path,
            source_stream_index=0,
            index_cache_dir=Path("/work/bestsource"),
            target_width=1920,
            target_height=1080,
            source_pix_fmt="yuv420p10le",
            source_color_transfer="bt709",
            source_color_primaries="bt709",
            source_color_space="bt709",
            source_hdr_metadata_present=False,
            hdr_to_sdr=True,
            identity_hash="identity-hash",
        ),
        av1an=Av1anCommandSpec(
            input_path=script_path,
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
            vapoursynth_script=script_path,
            av1an_command_json=Path("/work/av1an.command.json"),
            validation_policy_json=Path("/work/validation-policy.json"),
        ),
    )
