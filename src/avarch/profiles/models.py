from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProfileMatchSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    video_codec_not: list[str] = Field(default_factory=list)

    @field_validator("video_codec_not", mode="before")
    @classmethod
    def normalize_video_codecs(cls, value: object) -> list[str]:
        return _normalized_text_list(value)


class ProfileVideoSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_width: int = Field(gt=0)
    hdr_to_sdr: bool = False
    source: Literal["vapoursynth"] = "vapoursynth"

    @field_validator("max_width")
    @classmethod
    def require_even_max_width(cls, value: int) -> int:
        if value % 2 != 0:
            raise ValueError("max_width must be even")
        return value


class ProfileAv1anSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    encoder: Literal["svt-av1"]
    workers: int = Field(gt=0)
    video_args: str


class ProfileAudioSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    codec: str
    bitrate: str
    channels: int = Field(gt=0)
    languages: list[str]

    @field_validator("codec", mode="before")
    @classmethod
    def normalize_codec(cls, value: object) -> str:
        return str(value).strip().lower()

    @field_validator("languages", mode="before")
    @classmethod
    def normalize_languages(cls, value: object) -> list[str]:
        return _dedupe_preserving_order(_normalized_text_list(value))


class ProfileSubtitleSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    languages: list[str]
    keep_forced: bool = True

    @field_validator("languages", mode="before")
    @classmethod
    def normalize_languages(cls, value: object) -> list[str]:
        return _dedupe_preserving_order(_normalized_text_list(value))


class ProfileValidationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duration_tolerance_seconds: float = Field(default=2.0, ge=0)

    minimum_output_bytes: int = Field(default=1024, ge=1)
    minimum_output_source_ratio: float = Field(default=0.01, ge=0, le=1)

    decode_sample: bool = False
    decode_sample_seconds: float = Field(default=5.0, gt=0)

    minimum_size_reduction_percent: float | None = Field(default=None, ge=0, lt=100)


class EncodingProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backend: Literal["av1an"]
    container: Literal["mkv"]
    vapoursynth_template: Path | None = None
    match: ProfileMatchSettings
    video: ProfileVideoSettings
    av1an: ProfileAv1anSettings
    audio: ProfileAudioSettings
    subtitles: ProfileSubtitleSettings
    validation: ProfileValidationSettings = Field(default_factory=ProfileValidationSettings)


class ProfileDocument(EncodingProfile):
    name: str
    description: str | None = None
    tags: list[str] = Field(default_factory=list)
    known_limitations: list[str] = Field(default_factory=list)

    schema_version: Literal[1] = 1

    @field_validator("name", mode="before")
    @classmethod
    def normalize_name(cls, value: object) -> str:
        name = str(value).strip()
        if not name:
            raise ValueError("profile name must not be empty")
        return name

    def encoding_profile(self) -> EncodingProfile:
        payload = self.model_dump(
            exclude={"name", "description", "tags", "known_limitations", "schema_version"}
        )
        return EncodingProfile.model_validate(payload)


def _iterable_values(value: object) -> list[object]:
    if not isinstance(value, Iterable):
        msg = "value must be iterable"
        raise TypeError(msg)
    return list(cast(Iterable[object], value))


def _normalized_text_list(value: object) -> list[str]:
    if value is None:
        return []

    values = [value] if isinstance(value, str) else _iterable_values(value)
    return [str(item).strip().lower() for item in values]


def _dedupe_preserving_order(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result
