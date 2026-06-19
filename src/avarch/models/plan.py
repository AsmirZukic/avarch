from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from avarch.contracts import (
    AV1AN_COMMAND_SCHEMA_VERSION,
    AV1AN_STAGE_MARKER_SCHEMA_VERSION,
    ENCODE_RESULT_RECEIPT_SCHEMA_VERSION,
    EXECUTION_IDENTITY_SCHEMA_VERSION,
    FFMPEG_MUX_SCHEMA_VERSION,
    TRANSCODE_PLAN_SCHEMA_VERSION,
    VAPOURSYNTH_PLAN_SCHEMA_VERSION,
)
from avarch.models.promotion import PromotionPolicy
from avarch.models.validation import ValidationPolicy

AV1AN_COMMAND_CONTRACT_VERSION = 2
FFMPEG_MUX_CONTRACT_VERSION = 1

type VapourSynthMode = Literal[
    "generated",
    "custom_filter",
    "custom_template",
]

type VapourSynthOutputFormat = Literal["YUV420P10",]

type VapourSynthResizeFilter = Literal["spline36",]

type Av1anResumePolicy = Literal[
    "auto",
    "never",
]

type EncodeExecutionStatus = Literal[
    "completed",
    "already_complete",
]


class PlanArtifactPaths(BaseModel):
    artifact_dir: Path
    plan_json: Path
    vapoursynth_script: Path
    av1an_command_json: Path
    validation_policy_json: Path


class VideoPlan(BaseModel):
    source_stream_index: int = Field(ge=0)
    source_codec: str
    source_width: int
    source_height: int
    source_bit_depth: int | None
    source_pix_fmt: str | None
    source_color_transfer: str | None
    source_color_primaries: str | None
    source_color_space: str | None
    source_hdr_metadata_present: bool

    target_codec: Literal["av1"] = "av1"
    max_width: int
    target_width: int
    target_height: int
    resize_required: bool
    hdr_to_sdr: bool
    source: Literal["vapoursynth"]


class AudioPlan(BaseModel):
    source_stream_index: int = Field(ge=0)
    source_codec: str | None
    source_language: str | None
    source_channels: int | None
    source_title: str | None
    source_commentary: bool

    target_codec: str
    target_bitrate: str
    target_channels: int


class SubtitleStreamPlan(BaseModel):
    source_stream_index: int = Field(ge=0)
    codec: str | None
    language: str | None
    title: str | None
    forced: bool


class SubtitlePlan(BaseModel):
    streams: list[SubtitleStreamPlan]
    action: Literal["copy"] = "copy"


class ExecutionIdentity(BaseModel):
    schema_version: Literal[1] = EXECUTION_IDENTITY_SCHEMA_VERSION

    av1an_contract_version: int
    ffmpeg_mux_contract_version: int

    av1an_version_family: str

    video_container: Literal["mkv"]
    final_container: Literal["mkv"]

    identity_hash: str


class Av1anCommandSpec(BaseModel):
    schema_version: Literal[2] = AV1AN_COMMAND_SCHEMA_VERSION
    command_contract_version: int = AV1AN_COMMAND_CONTRACT_VERSION

    executable: str = "av1an"

    input_path: Path
    video_output_path: Path
    temp_dir: Path
    working_directory: Path

    encoder: Literal["svt-av1"]
    encoder_args: list[str]
    workers: int

    pixel_format: Literal["yuv420p10le"] = "yuv420p10le"
    concat_method: Literal["ffmpeg"] = "ffmpeg"
    cache_mode: Literal["temp"] = "temp"

    max_tries: int = 3

    no_defaults: Literal[True] = True
    keep_temp: Literal[True] = True
    never_overwrite: Literal[True] = True

    resume_policy: Av1anResumePolicy = "auto"


class FfmpegMuxSpec(BaseModel):
    schema_version: Literal[1] = FFMPEG_MUX_SCHEMA_VERSION
    command_contract_version: int = FFMPEG_MUX_CONTRACT_VERSION

    executable: str = "ffmpeg"

    video_input_path: Path
    source_input_path: Path
    output_path: Path

    audio_stream_index: int = Field(ge=0)
    subtitle_stream_indexes: list[int]

    audio_codec: str
    audio_bitrate: str
    audio_channels: int

    copy_subtitles: Literal[True] = True
    copy_chapters: Literal[True] = True
    copy_metadata: Literal[True] = True

    never_overwrite: Literal[True] = True


class ExecutionRuntimePaths(BaseModel):
    runtime_dir: Path

    av1an_stdout_log: Path
    av1an_stderr_log: Path

    mux_stdout_log: Path
    mux_stderr_log: Path

    av1an_stage_marker: Path
    encode_result: Path

    validation_report: Path

    validation_decode_stdout_log: Path
    validation_decode_stderr_log: Path


class Av1anStageMarker(BaseModel):
    schema_version: Literal[1] = AV1AN_STAGE_MARKER_SCHEMA_VERSION

    plan_hash: str
    av1an_spec_hash: str

    video_output_path: Path
    video_output_size: int

    completed_at: datetime


class EncodeResultReceipt(BaseModel):
    schema_version: Literal[1] = ENCODE_RESULT_RECEIPT_SCHEMA_VERSION

    plan_hash: str
    av1an_spec_hash: str
    mux_spec_hash: str

    video_output_path: Path
    video_output_size: int

    final_output_path: Path
    final_output_size: int

    completed_at: datetime


class VapourSynthPlan(BaseModel):
    schema_version: Literal[1] = VAPOURSYNTH_PLAN_SCHEMA_VERSION
    generator_version: int = 1

    mode: VapourSynthMode

    script_path: Path
    source_path: Path
    source_stream_index: int = Field(ge=0)

    index_cache_dir: Path

    target_width: int
    target_height: int

    output_format: VapourSynthOutputFormat = "YUV420P10"
    resize_filter: VapourSynthResizeFilter = "spline36"

    source_pix_fmt: str | None
    source_color_transfer: str | None
    source_color_primaries: str | None
    source_color_space: str | None
    source_hdr_metadata_present: bool

    hdr_to_sdr: bool

    template_path: Path | None = None
    template_hash: str | None = None
    filter_path: Path | None = None
    filter_hash: str | None = None
    filter_entrypoint: str | None = None
    filter_api_version: int | None = None

    environment_id: str | None = None
    environment_manifest_hash: str | None = None
    avarch_image_digest: str = "unknown"
    python_version: str | None = None
    python_abi: str | None = None
    runtime_platform: str | None = None
    vapoursynth_version: str | None = None
    template_api_version: int | None = None
    native_plugin_hashes: dict[str, str] = Field(default_factory=dict)

    identity_hash: str


class TranscodePlan(BaseModel):
    schema_version: Literal[5] = TRANSCODE_PLAN_SCHEMA_VERSION
    plan_hash: str

    input_path: Path
    output_path: Path
    temp_dir: Path

    media_file_id: int
    source_fs_fingerprint: str

    profile_name: str
    profile_hash: str
    probe_hash: str

    video: VideoPlan
    audio: AudioPlan
    subtitles: SubtitlePlan

    execution_identity: ExecutionIdentity
    vapoursynth: VapourSynthPlan
    av1an: Av1anCommandSpec
    mux: FfmpegMuxSpec
    runtime: ExecutionRuntimePaths
    validation: ValidationPolicy
    promotion: PromotionPolicy
    artifacts: PlanArtifactPaths

    @model_validator(mode="after")
    def required_execution_paths_must_match(self) -> TranscodePlan:
        if self.av1an.input_path != self.vapoursynth.script_path:
            raise ValueError("Av1an input_path must match VapourSynth script_path")
        if self.av1an.video_output_path == self.output_path:
            raise ValueError("Av1an video_output_path must differ from final output_path")
        if self.mux.video_input_path != self.av1an.video_output_path:
            raise ValueError("Mux video_input_path must match Av1an video_output_path")
        if self.mux.source_input_path != self.input_path:
            raise ValueError("Mux source_input_path must match plan input_path")
        if self.mux.output_path != self.output_path:
            raise ValueError("Mux output_path must match plan output_path")
        return self
