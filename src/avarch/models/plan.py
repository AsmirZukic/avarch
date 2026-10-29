from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast

from pydantic import (
    BaseModel,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from avarch.contracts import (
    AV1AN_COMMAND_SCHEMA_VERSION,
    AV1AN_STAGE_MARKER_SCHEMA_VERSION,
    ENCODE_RESULT_RECEIPT_SCHEMA_VERSION,
    EXECUTION_IDENTITY_SCHEMA_VERSION,
    FFMPEG_MUX_SCHEMA_VERSION,
    NVENC_COMMAND_SCHEMA_VERSION,
    NVENC_STAGE_MARKER_SCHEMA_VERSION,
    VAPOURSYNTH_PLAN_SCHEMA_VERSION,
)
from avarch.models.promotion import PromotionPolicy
from avarch.models.validation import ValidationPolicy

AV1AN_COMMAND_CONTRACT_VERSION = 2
NVENC_COMMAND_CONTRACT_VERSION = 2
FFMPEG_MUX_CONTRACT_VERSION = 1


def _serialized_without_none(payload: object) -> object:
    if not isinstance(payload, dict):
        return payload
    typed_payload = cast(dict[str, Any], payload)
    return {key: value for key, value in typed_payload.items() if value is not None}


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
    av1an_command_json: Path = cast(Path, None)
    nvenc_command_json: Path = cast(Path, None)
    validation_policy_json: Path

    @model_serializer
    def serialize_model(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "artifact_dir": self.artifact_dir,
            "plan_json": self.plan_json,
            "vapoursynth_script": self.vapoursynth_script,
            "validation_policy_json": self.validation_policy_json,
        }
        av1an_path = cast(Path | None, self.__dict__.get("av1an_command_json"))
        nvenc_path = cast(Path | None, self.__dict__.get("nvenc_command_json"))
        if av1an_path is not None:
            payload["av1an_command_json"] = av1an_path
        if nvenc_path is not None:
            payload["nvenc_command_json"] = nvenc_path
        return payload


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

    av1an_contract_version: int | None = None
    nvenc_contract_version: int | None = None
    ffmpeg_mux_contract_version: int

    av1an_version_family: str | None = None
    backend: Literal["av1an", "ffmpeg_nvenc"] | None = None

    video_container: Literal["mkv"]
    final_container: Literal["mkv"]

    identity_hash: str

    @model_serializer(mode="wrap")
    def serialize_model(self, handler: SerializerFunctionWrapHandler) -> object:
        return _serialized_without_none(handler(self))


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


class NvencToneMappingSpec(BaseModel):
    processor: Literal["opencl"] = "opencl"
    algorithm: Literal["linear", "gamma", "clip", "reinhard", "hable", "mobius"] = "mobius"
    peak: float = Field(default=0, ge=0)
    param: float = Field(default=0.3, gt=0)
    desaturation: float = Field(default=0.5, ge=0)
    scene_threshold: float = Field(default=0.2, ge=0)

    source_color_primaries: str
    source_color_transfer: str
    source_color_space: str
    source_color_range: Literal["tv", "pc"] = "tv"


class NvencCommandSpec(BaseModel):
    schema_version: Literal[1] = NVENC_COMMAND_SCHEMA_VERSION
    command_contract_version: int = NVENC_COMMAND_CONTRACT_VERSION

    ffmpeg_executable: str = "ffmpeg"
    vspipe_executable: str = "vspipe"

    input_path: Path
    video_output_path: Path
    working_directory: Path

    encoder: Literal["av1_nvenc"] = "av1_nvenc"
    preset: Literal["p1", "p2", "p3", "p4", "p5", "p6", "p7"] = "p5"
    tune: Literal["hq", "uhq"] = "hq"
    cq: float = Field(default=28, ge=1, le=63)
    multipass: Literal["disabled", "qres", "fullres"] = "qres"
    lookahead: int = Field(default=16, ge=0, le=32)
    spatial_aq: bool = True
    temporal_aq: bool = True
    aq_strength: int = Field(default=8, ge=1, le=15)
    gpu: int = Field(default=0, ge=0)

    pixel_format: Literal["p010le"] = "p010le"
    color_primaries: Literal["bt709"] = "bt709"
    color_transfer: Literal["bt709"] = "bt709"
    color_space: Literal["bt709"] = "bt709"
    color_range: Literal["tv"] = "tv"
    tone_mapping: NvencToneMappingSpec | None = None

    never_overwrite: Literal[True] = True


class FfmpegMuxSpec(BaseModel):
    schema_version: Literal[1] = FFMPEG_MUX_SCHEMA_VERSION
    command_contract_version: int = FFMPEG_MUX_CONTRACT_VERSION

    executable: str = "ffmpeg"

    video_input_path: Path
    source_input_path: Path
    output_path: Path

    audio_stream_index: int | None = Field(default=None, ge=0)
    subtitle_stream_indexes: list[int]

    audio_codec: str | None = None
    audio_bitrate: str | None = None
    audio_channels: int | None = None

    copy_subtitles: Literal[True] = True
    copy_chapters: Literal[True] = True
    copy_metadata: Literal[True] = True

    never_overwrite: Literal[True] = True


class ExecutionRuntimePaths(BaseModel):
    runtime_dir: Path

    av1an_stdout_log: Path = cast(Path, None)
    av1an_stderr_log: Path = cast(Path, None)
    vspipe_stderr_log: Path = cast(Path, None)
    nvenc_stdout_log: Path = cast(Path, None)
    nvenc_stderr_log: Path = cast(Path, None)

    mux_stdout_log: Path
    mux_stderr_log: Path

    av1an_stage_marker: Path = cast(Path, None)
    nvenc_stage_marker: Path = cast(Path, None)
    encode_result: Path

    validation_report: Path

    validation_decode_stdout_log: Path
    validation_decode_stderr_log: Path

    @model_serializer
    def serialize_model(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "runtime_dir": self.runtime_dir,
            "mux_stdout_log": self.mux_stdout_log,
            "mux_stderr_log": self.mux_stderr_log,
            "encode_result": self.encode_result,
            "validation_report": self.validation_report,
            "validation_decode_stdout_log": self.validation_decode_stdout_log,
            "validation_decode_stderr_log": self.validation_decode_stderr_log,
        }
        for field_name in (
            "av1an_stdout_log",
            "av1an_stderr_log",
            "vspipe_stderr_log",
            "nvenc_stdout_log",
            "nvenc_stderr_log",
            "av1an_stage_marker",
            "nvenc_stage_marker",
        ):
            path = cast(Path | None, self.__dict__.get(field_name))
            if path is not None:
                payload[field_name] = path
        return payload


class Av1anStageMarker(BaseModel):
    schema_version: Literal[1, 2] = AV1AN_STAGE_MARKER_SCHEMA_VERSION

    backend: Literal["av1an"] | None = None
    plan_hash: str
    av1an_spec_hash: str

    video_output_path: Path
    video_output_size: int

    completed_at: datetime

    @model_serializer(mode="wrap")
    def serialize_model(self, handler: SerializerFunctionWrapHandler) -> object:
        return _serialized_without_none(handler(self))


class NvencStageMarker(BaseModel):
    schema_version: Literal[1] = NVENC_STAGE_MARKER_SCHEMA_VERSION

    backend: Literal["ffmpeg_nvenc"] = "ffmpeg_nvenc"
    plan_hash: str
    encoder_spec_hash: str

    video_output_path: Path
    video_output_size: int

    completed_at: datetime


class EncodeResultReceipt(BaseModel):
    schema_version: Literal[1, 2] = ENCODE_RESULT_RECEIPT_SCHEMA_VERSION

    plan_hash: str
    av1an_spec_hash: str | None = None
    backend: Literal["av1an", "ffmpeg_nvenc"] | None = None
    encoder_spec_hash: str | None = None
    mux_spec_hash: str

    video_output_path: Path
    video_output_size: int

    final_output_path: Path
    final_output_size: int

    completed_at: datetime

    @model_serializer(mode="wrap")
    def serialize_model(self, handler: SerializerFunctionWrapHandler) -> object:
        return _serialized_without_none(handler(self))


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
    schema_version: Literal[5, 6] = 5
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
    audio: AudioPlan | None
    subtitles: SubtitlePlan

    execution_identity: ExecutionIdentity
    vapoursynth: VapourSynthPlan
    backend: Literal["av1an", "ffmpeg_nvenc"] | None = None
    av1an: Av1anCommandSpec = cast(Av1anCommandSpec, None)
    nvenc: NvencCommandSpec = cast(NvencCommandSpec, None)
    mux: FfmpegMuxSpec
    runtime: ExecutionRuntimePaths
    validation: ValidationPolicy
    promotion: PromotionPolicy
    artifacts: PlanArtifactPaths

    @property
    def encoding_backend(self) -> Literal["av1an", "ffmpeg_nvenc"]:
        return self.backend or "av1an"

    @property
    def video_output_path(self) -> Path:
        if self.encoding_backend == "av1an":
            return self.av1an.video_output_path
        return self.nvenc.video_output_path

    @model_validator(mode="after")
    def required_execution_paths_must_match(self) -> TranscodePlan:
        av1an = cast(Av1anCommandSpec | None, self.__dict__.get("av1an"))
        nvenc = cast(NvencCommandSpec | None, self.__dict__.get("nvenc"))
        if self.schema_version == 5:
            if self.backend not in {None, "av1an"} or av1an is None or nvenc is not None:
                raise ValueError("Version 5 plans must contain only an Av1an command")
        elif self.backend == "av1an":
            if av1an is None or nvenc is not None:
                raise ValueError("Av1an plans require only an Av1an command")
        elif self.backend == "ffmpeg_nvenc":
            if nvenc is None or av1an is not None:
                raise ValueError("NVENC plans require only an NVENC command")
        else:
            raise ValueError("Version 6 plans require an explicit backend")

        encoder = av1an if self.encoding_backend == "av1an" else nvenc
        if encoder is None:
            raise ValueError("Plan is missing its video encoder command")
        if encoder.input_path != self.vapoursynth.script_path:
            raise ValueError("Encoder input_path must match VapourSynth script_path")
        if encoder.video_output_path == self.output_path:
            raise ValueError("Video output path must differ from final output_path")
        if self.mux.video_input_path != encoder.video_output_path:
            raise ValueError("Mux video_input_path must match encoder video_output_path")
        if self.mux.source_input_path != self.input_path:
            raise ValueError("Mux source_input_path must match plan input_path")
        if self.mux.output_path != self.output_path:
            raise ValueError("Mux output_path must match plan output_path")
        return self

    @model_serializer(mode="wrap")
    def serialize_model(self, handler: SerializerFunctionWrapHandler) -> object:
        serialized = handler(self)
        if not isinstance(serialized, dict):
            return serialized
        payload = cast(dict[str, Any], serialized)
        if self.schema_version == 5:
            payload.pop("backend", None)
            payload.pop("nvenc", None)
        else:
            payload.pop("av1an", None)
        return payload
