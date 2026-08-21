from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from avarch.contracts import NORMALIZED_PROBE_SCHEMA_VERSION


def _video_streams() -> list[VideoStream]:
    return []


def _audio_streams() -> list[AudioStream]:
    return []


def _subtitle_streams() -> list[SubtitleStream]:
    return []


def _attachments() -> list[Attachment]:
    return []


def _chapters() -> list[Chapter]:
    return []


class VideoStream(BaseModel):
    index: int = Field(ge=0)
    codec: str | None = None
    width: int | None = Field(default=None, ge=0)
    height: int | None = Field(default=None, ge=0)
    fps: str | None = None
    bit_depth: int | None = Field(default=None, ge=0)
    pix_fmt: str | None = None
    color_transfer: str | None = None
    color_primaries: str | None = None
    color_space: str | None = None
    hdr_metadata_present: bool = False


class AudioStream(BaseModel):
    index: int = Field(ge=0)
    codec: str | None = None
    language: str | None = None
    channels: int | None = Field(default=None, ge=0)
    title: str | None = None
    default: bool = False
    forced: bool = False
    commentary: bool = False


class SubtitleStream(BaseModel):
    index: int = Field(ge=0)
    codec: str | None = None
    language: str | None = None
    title: str | None = None
    default: bool = False
    forced: bool = False


class Attachment(BaseModel):
    index: int = Field(ge=0)
    codec: str | None = None
    filename: str | None = None


class Chapter(BaseModel):
    id: int
    start_seconds: float | None = None
    title: str | None = None


class NormalizedProbe(BaseModel):
    schema_version: Literal[1] = NORMALIZED_PROBE_SCHEMA_VERSION
    container: str | None = None
    duration_seconds: float | None = None
    bitrate_bps: int | None = None
    video_streams: list[VideoStream] = Field(default_factory=_video_streams)
    audio_streams: list[AudioStream] = Field(default_factory=_audio_streams)
    subtitle_streams: list[SubtitleStream] = Field(default_factory=_subtitle_streams)
    attachments: list[Attachment] = Field(default_factory=_attachments)
    chapters: list[Chapter] = Field(default_factory=_chapters)
