from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


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
    source_hdr_metadata_present: bool

    target_codec: Literal["av1"] = "av1"
    max_width: int
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


class Av1anCommandSpec(BaseModel):
    schema_version: int = 1
    executable: str = "av1an"
    input_path: Path
    output_path: Path
    temp_dir: Path
    encoder: str
    encoder_args: list[str]
    workers: int
    resume: bool = True


class ValidationPolicy(BaseModel):
    schema_version: int = 1
    expected_container: str
    expected_video_codec: Literal["av1"] = "av1"
    maximum_width: int
    expected_audio_codec: str
    expected_audio_channels: int
    expected_subtitle_streams: list[int]
    source_duration_seconds: float | None


class PromotionPolicy(BaseModel):
    mode: Literal["manual_review"] = "manual_review"


class TranscodePlan(BaseModel):
    schema_version: int = 1
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

    av1an: Av1anCommandSpec
    validation: ValidationPolicy
    promotion: PromotionPolicy = Field(default_factory=PromotionPolicy)
    artifacts: PlanArtifactPaths
