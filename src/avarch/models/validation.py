from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from avarch.models.probe import AudioStream, SubtitleStream, VideoStream

JsonValue = Any


class ExpectedSubtitlePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_order: int = Field(ge=0)
    source_stream_index: int = Field(ge=0)

    codec: str | None
    language: str | None
    forced: bool


class DecodeSamplePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    duration_seconds: float = Field(default=5.0, gt=0)


class ValidationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    policy_hash: str

    accepted_container_names: list[str]

    source_duration_seconds: float
    source_size_bytes: int = Field(ge=0)

    duration_tolerance_seconds: float = Field(ge=0)

    expected_video_stream_count: int = Field(default=1, ge=0)
    expected_video_codec: Literal["av1"] = "av1"
    expected_width: int = Field(gt=0)
    expected_height: int = Field(gt=0)

    expected_audio_stream_count: int = Field(default=1, ge=0)
    expected_audio_codec: str
    expected_audio_channels: int = Field(gt=0)
    expected_audio_language: str | None

    expected_subtitles: list[ExpectedSubtitlePolicy]

    minimum_output_bytes: int = Field(ge=1)
    minimum_output_source_ratio: float = Field(ge=0, le=1)

    minimum_size_reduction_percent: float | None = Field(default=None, ge=0, lt=100)

    decode_sample: DecodeSamplePolicy


class ValidationCheckStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    WARNING = "warning"
    SKIPPED = "skipped"


class ValidationCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    status: ValidationCheckStatus

    required: bool

    expected: JsonValue | None = None
    observed: JsonValue | None = None

    message: str | None = None


class ObservedValidationMedia(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_size_bytes: int | None

    container: str | None
    duration_seconds: float | None

    video_streams: list[VideoStream]
    audio_streams: list[AudioStream]
    subtitle_streams: list[SubtitleStream]


class ValidationReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1

    plan_hash: str
    policy_hash: str

    source_path: Path
    output_path: Path

    source_fs_fingerprint_before: str | None
    source_fs_fingerprint_after: str | None

    output_fs_fingerprint_before: str | None
    output_fs_fingerprint_after: str | None

    passed: bool

    checks: list[ValidationCheck]
    warnings: list[str]

    observed: ObservedValidationMedia | None

    started_at: datetime
    finished_at: datetime


def checks_pass(checks: list[ValidationCheck]) -> bool:
    return all(
        check.status == ValidationCheckStatus.PASS
        for check in checks
        if check.required
    )
