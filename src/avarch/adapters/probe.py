from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Mapping
from fractions import Fraction
from pathlib import Path
from typing import Any, cast

import structlog
from pydantic import ValidationError

from avarch.models.probe import (
    Attachment,
    AudioStream,
    Chapter,
    NormalizedProbe,
    SubtitleStream,
    VideoStream,
)
from avarch.serialization import canonical_json

log = structlog.get_logger(__name__)

_MAX_STDERR_LENGTH = 4096
_HDR_SIDE_DATA_TYPES = {
    "mastering display metadata",
    "content light level metadata",
    "hdr dynamic metadata smpte2094-40",
    "dovi configuration record",
}
_KNOWN_8_BIT_PIXEL_FORMATS = {
    "gray",
    "nv12",
    "nv21",
    "yuv410p",
    "yuv411p",
    "yuv420p",
    "yuv422p",
    "yuv440p",
    "yuv444p",
    "yuyv422",
}


class ProbeError(RuntimeError):
    pass


class ProbeExecutableNotFoundError(ProbeError):
    pass


class ProbeTimeoutError(ProbeError):
    pass


class ProbeProcessError(ProbeError):
    pass


class ProbeOutputError(ProbeError):
    pass


class ProbeNormalizationError(ProbeError):
    pass


class FfprobeCollector:
    def __init__(self, *, probe_runner: Any | None = None) -> None:
        self._probe_runner = run_ffprobe if probe_runner is None else probe_runner

    def collect(self, path: Path) -> tuple[Mapping[str, Any], NormalizedProbe]:
        raw_probe = self._probe_runner(path)
        normalized_probe = normalize_probe(raw_probe)
        return raw_probe, normalized_probe


def build_ffprobe_command(path: Path, *, executable: str = "ffprobe") -> list[str]:
    return [
        executable,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        "-show_chapters",
        str(path),
    ]


def run_ffprobe(
    path: Path,
    *,
    executable: str = "ffprobe",
    timeout_seconds: float = 120.0,
) -> dict[str, Any]:
    command = build_ffprobe_command(path, executable=executable)
    log.info("probe_started", path=str(path))
    log.debug("probe_command", command=command)

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as exc:
        log.error("probe_failed", path=str(path), reason="executable_not_found")
        raise ProbeExecutableNotFoundError(f"ffprobe executable not found: {executable}") from exc
    except subprocess.TimeoutExpired as exc:
        log.error("probe_failed", path=str(path), reason="timeout")
        raise ProbeTimeoutError(f"ffprobe timed out after {timeout_seconds:g} seconds") from exc

    if result.returncode != 0:
        stderr = _truncate(result.stderr.strip())
        log.error("probe_failed", path=str(path), reason="process_error")
        raise ProbeProcessError(f"ffprobe failed with exit code {result.returncode}: {stderr}")

    if not result.stdout.strip():
        log.error("probe_failed", path=str(path), reason="empty_output")
        raise ProbeOutputError("ffprobe produced no JSON output")

    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        log.error("probe_failed", path=str(path), reason="invalid_json")
        raise ProbeOutputError("ffprobe produced invalid JSON") from exc

    if not isinstance(raw, dict):
        log.error("probe_failed", path=str(path), reason="non_object_json")
        raise ProbeOutputError("ffprobe JSON output must be an object")

    log.info("probe_completed", path=str(path))
    return cast(dict[str, Any], raw)


def normalize_probe(raw: Mapping[str, Any]) -> NormalizedProbe:
    format_section = _mapping_or_empty(raw.get("format"))
    streams = _sequence(raw.get("streams"))
    chapters = _sequence(raw.get("chapters"))

    video_streams: list[VideoStream] = []
    audio_streams: list[AudioStream] = []
    subtitle_streams: list[SubtitleStream] = []
    attachments: list[Attachment] = []

    for stream_value in streams:
        if not isinstance(stream_value, Mapping):
            continue
        stream = cast(Mapping[str, Any], stream_value)
        codec_type = stream.get("codec_type")
        if codec_type == "video":
            video_streams.append(_normalize_video_stream(stream))
        elif codec_type == "audio":
            audio_streams.append(_normalize_audio_stream(stream))
        elif codec_type == "subtitle":
            subtitle_streams.append(_normalize_subtitle_stream(stream))
        elif codec_type == "attachment":
            attachments.append(_normalize_attachment(stream))

    normalized_chapters = [
        _normalize_chapter(cast(Mapping[str, Any], chapter))
        for chapter in chapters
        if isinstance(chapter, Mapping)
    ]

    try:
        return NormalizedProbe(
            container=_clean_text(format_section.get("format_name")),
            duration_seconds=_parse_optional_float(
                format_section.get("duration"),
                allow_negative=False,
            ),
            bitrate_bps=_parse_optional_int(format_section.get("bit_rate"), allow_negative=False),
            video_streams=sorted(video_streams, key=lambda stream: stream.index),
            audio_streams=sorted(audio_streams, key=lambda stream: stream.index),
            subtitle_streams=sorted(subtitle_streams, key=lambda stream: stream.index),
            attachments=sorted(attachments, key=lambda stream: stream.index),
            chapters=sorted(
                normalized_chapters,
                key=lambda chapter: (
                    float("inf") if chapter.start_seconds is None else chapter.start_seconds,
                    chapter.id,
                ),
            ),
        )
    except ValidationError as exc:
        raise ProbeNormalizationError("Unable to normalize ffprobe metadata") from exc


def build_probe_hash(normalized: NormalizedProbe) -> str:
    normalized_json = canonical_json(normalized)
    payload = b"probe-v1\0" + normalized_json.encode("utf-8")
    return hashlib.blake2b(payload, digest_size=32).hexdigest()


def _normalize_video_stream(stream: Mapping[str, Any]) -> VideoStream:
    return VideoStream(
        index=_required_stream_index(stream),
        codec=_clean_text(stream.get("codec_name")),
        width=_parse_optional_int(stream.get("width"), allow_negative=False),
        height=_parse_optional_int(stream.get("height"), allow_negative=False),
        fps=_normalize_frame_rate(stream.get("avg_frame_rate"))
        or _normalize_frame_rate(stream.get("r_frame_rate")),
        bit_depth=_infer_bit_depth(stream),
        pix_fmt=_clean_text(stream.get("pix_fmt")),
        color_transfer=_clean_text(stream.get("color_transfer")),
        color_primaries=_clean_text(stream.get("color_primaries")),
        color_space=_clean_text(stream.get("color_space")),
        hdr_metadata_present=_has_hdr_metadata(stream),
    )


def _normalize_audio_stream(stream: Mapping[str, Any]) -> AudioStream:
    tags = _mapping_or_empty(stream.get("tags"))
    title = _clean_text(tags.get("title"))
    return AudioStream(
        index=_required_stream_index(stream),
        codec=_clean_text(stream.get("codec_name")),
        language=_normalize_language(tags.get("language")),
        channels=_parse_optional_int(stream.get("channels"), allow_negative=False),
        title=title,
        default=_parse_bool(_mapping_or_empty(stream.get("disposition")).get("default")),
        forced=_parse_bool(_mapping_or_empty(stream.get("disposition")).get("forced")),
        commentary=title is not None and "commentary" in title.casefold(),
    )


def _normalize_subtitle_stream(stream: Mapping[str, Any]) -> SubtitleStream:
    tags = _mapping_or_empty(stream.get("tags"))
    disposition = _mapping_or_empty(stream.get("disposition"))
    return SubtitleStream(
        index=_required_stream_index(stream),
        codec=_clean_text(stream.get("codec_name")),
        language=_normalize_language(tags.get("language")),
        title=_clean_text(tags.get("title")),
        default=_parse_bool(disposition.get("default")),
        forced=_parse_bool(disposition.get("forced")),
    )


def _normalize_attachment(stream: Mapping[str, Any]) -> Attachment:
    tags = _mapping_or_empty(stream.get("tags"))
    return Attachment(
        index=_required_stream_index(stream),
        codec=_clean_text(stream.get("codec_name")),
        filename=_clean_text(tags.get("filename")),
        mimetype=_clean_text(tags.get("mimetype")),
    )


def _normalize_chapter(chapter: Mapping[str, Any]) -> Chapter:
    chapter_id = _parse_optional_int(chapter.get("id"), allow_negative=True)
    if chapter_id is None:
        raise ProbeNormalizationError("Recognized chapter is missing a valid id")
    return Chapter(
        id=chapter_id,
        start_seconds=_parse_optional_float(chapter.get("start_time"), allow_negative=False),
        end_seconds=_parse_optional_float(chapter.get("end_time"), allow_negative=False),
        title=_clean_text(_mapping_or_empty(chapter.get("tags")).get("title")),
    )


def _required_stream_index(stream: Mapping[str, Any]) -> int:
    index = _parse_optional_int(stream.get("index"), allow_negative=False)
    if index is None:
        raise ProbeNormalizationError("Recognized stream is missing a valid index")
    return index


def _normalize_frame_rate(value: object) -> str | None:
    text = _clean_text(value)
    if text is None or text == "N/A":
        return None
    try:
        fraction = Fraction(text)
    except (ValueError, ZeroDivisionError):
        return None
    if fraction <= 0:
        return None
    return f"{fraction.numerator}/{fraction.denominator}"


def _infer_bit_depth(stream: Mapping[str, Any]) -> int | None:
    for key in ("bits_per_raw_sample", "bits_per_sample"):
        value = _parse_optional_int(stream.get(key), allow_negative=False)
        if value is not None:
            return value

    pix_fmt = _clean_text(stream.get("pix_fmt"))
    if pix_fmt is None:
        return None

    marker_index = pix_fmt.find("p")
    if marker_index >= 0:
        suffix = pix_fmt[marker_index + 1 :]
        digits: list[str] = []
        for character in suffix:
            if character.isdigit():
                digits.append(character)
            elif digits:
                break
        if digits:
            return int("".join(digits))

    if pix_fmt in _KNOWN_8_BIT_PIXEL_FORMATS:
        return 8
    return None


def _has_hdr_metadata(stream: Mapping[str, Any]) -> bool:
    for side_data in _sequence(stream.get("side_data_list")):
        if not isinstance(side_data, Mapping):
            continue
        side_data_mapping = cast(Mapping[str, Any], side_data)
        side_data_type = _clean_text(side_data_mapping.get("side_data_type"))
        if side_data_type is not None and side_data_type.casefold() in _HDR_SIDE_DATA_TYPES:
            return True
    return False


def _parse_optional_int(value: object, *, allow_negative: bool) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        parsed = int(value)
    else:
        text = _clean_text(value)
        if text is None or text == "N/A":
            return None
        try:
            parsed = int(text)
        except ValueError:
            try:
                parsed_float = float(text)
            except ValueError:
                return None
            if not parsed_float.is_integer():
                return None
            parsed = int(parsed_float)
    if parsed < 0 and not allow_negative:
        return None
    return parsed


def _parse_optional_float(value: object, *, allow_negative: bool) -> float | None:
    text = _clean_text(value)
    if text is None or text == "N/A":
        return None
    try:
        parsed = float(text)
    except ValueError:
        return None
    if parsed < 0 and not allow_negative:
        return None
    return parsed


def _parse_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    text = _clean_text(value)
    if text is None:
        return False
    return text.casefold() in {"1", "true", "yes", "y", "on"}


def _normalize_language(value: object) -> str | None:
    text = _clean_text(value)
    if text is None:
        return None
    return text.lower()


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text


def _mapping_or_empty(value: object) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return cast(Mapping[str, Any], value)
    return {}


def _sequence(value: object) -> list[object]:
    if isinstance(value, list):
        return cast(list[object], value)
    return []


def _truncate(value: str) -> str:
    if len(value) <= _MAX_STDERR_LENGTH:
        return value
    return f"{value[:_MAX_STDERR_LENGTH]}..."
