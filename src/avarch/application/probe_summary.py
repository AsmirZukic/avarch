from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from avarch.models.probe import NormalizedProbe


class ProbeSummaryError(RuntimeError):
    pass


def parse_normalized_probe_json(value: str) -> NormalizedProbe:
    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ProbeSummaryError("Stored normalized probe JSON is invalid") from exc
    try:
        normalized = NormalizedProbe.model_validate(data)
    except ValidationError as exc:
        raise ProbeSummaryError("Stored normalized probe JSON is not supported") from exc
    return normalized


def format_probe_summary(path: Path, normalized: NormalizedProbe, probe_hash: str) -> str:
    lines = [
        f"File: {path}",
        f"Container: {_display(normalized.container)}",
        f"Duration: {_display_seconds(normalized.duration_seconds)}",
        f"Bitrate: {_display_bitrate(normalized.bitrate_bps)}",
        "",
        "Video streams:",
    ]

    for stream in sorted(normalized.video_streams, key=lambda item: item.index):
        lines.append(
            "  "
            f"[{stream.index}] {_display(stream.codec)} "
            f"{_display_dimensions(stream.width, stream.height)} "
            f"{_display(stream.fps)} fps "
            f"{_display_bit_depth(stream.bit_depth)} "
            f"{_display(stream.pix_fmt)}"
        )
        lines.append(
            "      "
            f"color={_display(stream.color_space)}/{_display(stream.color_primaries)}/"
            f"{_display(stream.color_transfer)} "
            f"hdr_metadata={'yes' if stream.hdr_metadata_present else 'no'}"
        )

    lines.append("")
    lines.append("Audio streams:")
    for stream in sorted(normalized.audio_streams, key=lambda item: item.index):
        flags = _stream_flags(
            default=stream.default,
            forced=stream.forced,
            commentary=stream.commentary,
        )
        lines.append(
            "  "
            f"[{stream.index}] {_display(stream.codec)} {_display(stream.language)} "
            f"{_display_channels(stream.channels)}{flags}"
        )

    lines.append("")
    lines.append("Subtitle streams:")
    for stream in sorted(normalized.subtitle_streams, key=lambda item: item.index):
        flags = _stream_flags(default=stream.default, forced=stream.forced)
        lines.append(
            f"  [{stream.index}] {_display(stream.codec)} {_display(stream.language)}{flags}"
        )

    lines.extend(
        [
            "",
            f"Attachments: {len(normalized.attachments)}",
            f"Chapters: {len(normalized.chapters)}",
            f"Probe hash: {probe_hash}",
        ]
    )
    return "\n".join(lines)


def _display(value: object | None) -> str:
    if value is None:
        return "-"
    return str(value)


def _display_seconds(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:g} s"


def _display_bitrate(value: int | None) -> str:
    if value is None:
        return "-"
    return f"{value} bps"


def _display_dimensions(width: int | None, height: int | None) -> str:
    if width is None or height is None:
        return "-"
    return f"{width}x{height}"


def _display_bit_depth(value: int | None) -> str:
    if value is None:
        return "-"
    return f"{value}-bit"


def _display_channels(value: int | None) -> str:
    if value is None:
        return "- channels"
    return f"{value} channels"


def _stream_flags(*, default: bool, forced: bool, commentary: bool = False) -> str:
    flags: list[str] = []
    if default:
        flags.append("default")
    if forced:
        flags.append("forced")
    if commentary:
        flags.append("commentary")
    if not flags:
        return ""
    return " " + " ".join(flags)
