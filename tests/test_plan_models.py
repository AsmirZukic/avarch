from pathlib import Path

import pytest
from pydantic import ValidationError

from avarch.models.plan import (
    AudioPlan,
    Av1anCommandSpec,
    ExecutionIdentity,
    ExecutionRuntimePaths,
    FfmpegMuxSpec,
    PlanArtifactPaths,
    SubtitlePlan,
    SubtitleStreamPlan,
    TranscodePlan,
    ValidationPolicy,
    VapourSynthPlan,
    VideoPlan,
)
from avarch.models.promotion import PromotionPolicy
from avarch.models.validation import DecodeSamplePolicy, ExpectedSubtitlePolicy


def test_plan_serializes_paths_as_strings() -> None:
    plan = sample_plan()

    data = plan.model_dump(mode="json")

    assert data["input_path"] == "/media/movie.mkv"
    assert data["artifacts"]["plan_json"] == "/work/plan.json"


def test_plan_round_trips_through_json() -> None:
    plan = sample_plan()

    restored = TranscodePlan.model_validate_json(plan.model_dump_json())

    assert restored == plan


def test_plan_requires_promotion_policy() -> None:
    data = sample_plan().model_dump()
    data.pop("promotion")

    with pytest.raises(ValidationError):
        TranscodePlan.model_validate(data)


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


def test_av1an_spec_defaults_to_one_worker() -> None:
    spec = Av1anCommandSpec(
        input_path=Path("/media/movie.mkv"),
        video_output_path=Path("/output/video-only.mkv"),
        temp_dir=Path("/work/temp"),
        working_directory=Path("/work"),
        encoder="svt-av1",
        encoder_args=["--crf", "28"],
    )

    assert spec.workers == 1


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
    work_dir = Path("/work")
    video_output_path = work_dir / "video-only.mkv"
    av1an_temp_dir = work_dir / "av1an"
    runtime_dir = work_dir / "runtime"
    script_path = Path("/work/movie.vpy")
    return TranscodePlan(
        plan_hash="plan-hash",
        semantic_hash="semantic-hash",
        input_path=input_path,
        output_path=output_path,
        temp_dir=work_dir,
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
        execution_identity=ExecutionIdentity(
            av1an_contract_version=4,
            ffmpeg_mux_contract_version=1,
            av1an_version_family="0.5.x",
            final_container="mkv",
            identity_hash="identity-hash",
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
            video_output_path=video_output_path,
            temp_dir=av1an_temp_dir,
            working_directory=work_dir,
            encoder="svt-av1",
            encoder_args=["--preset", "6", "--crf", "28"],
            workers=6,
        ),
        mux=FfmpegMuxSpec(
            video_input_path=video_output_path,
            source_input_path=input_path,
            output_path=output_path,
            audio_stream_index=1,
            subtitle_stream_indexes=[2],
            audio_codec="libopus",
            audio_bitrate="128k",
            audio_channels=2,
        ),
        runtime=ExecutionRuntimePaths(
            runtime_dir=runtime_dir,
            av1an_stdout_log=runtime_dir / "av1an.stdout.log",
            av1an_stderr_log=runtime_dir / "av1an.stderr.log",
            mux_stdout_log=runtime_dir / "mux.stdout.log",
            mux_stderr_log=runtime_dir / "mux.stderr.log",
            av1an_stage_marker=runtime_dir / "av1an-stage.json",
            encode_result=runtime_dir / "encode-result.json",
            validation_report=runtime_dir / "validation-report.json",
            validation_decode_stdout_log=runtime_dir / "validation.decode.stdout.log",
            validation_decode_stderr_log=runtime_dir / "validation.decode.stderr.log",
        ),
        validation=ValidationPolicy(
            policy_hash="policy-hash",
            accepted_container_names=["matroska,webm"],
            source_duration_seconds=600.0,
            source_size_bytes=1024,
            duration_tolerance_seconds=2.0,
            expected_width=1920,
            expected_height=1080,
            expected_audio_codec="opus",
            expected_audio_channels=2,
            expected_audio_language="eng",
            expected_subtitles=[
                ExpectedSubtitlePolicy(
                    output_order=0,
                    source_stream_index=2,
                    codec="subrip",
                    language="eng",
                    forced=False,
                )
            ],
            minimum_output_bytes=1024,
            minimum_output_source_ratio=0.01,
            minimum_size_reduction_percent=None,
            decode_sample=DecodeSamplePolicy(enabled=False, duration_seconds=5.0),
        ),
        promotion=PromotionPolicy(policy_hash="promotion-policy-hash"),
        artifacts=PlanArtifactPaths(
            artifact_dir=Path("/work"),
            plan_json=Path("/work/plan.json"),
            vapoursynth_script=script_path,
            av1an_command_json=Path("/work/av1an.command.json"),
            validation_policy_json=Path("/work/validation-policy.json"),
        ),
    )
