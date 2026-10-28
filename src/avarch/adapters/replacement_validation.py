"""Conservative, full-file evidence required before replacing original media.

This proves decodability and video timeline coverage, not perceptual equivalence.
The metadata-only validator remains useful for non-destructive viewing copies.
"""

from __future__ import annotations

import math
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

from avarch.domain.media_coverage import AudioCoverage, audio_coverage_matches


class ReplacementValidationError(RuntimeError):
    pass


def _run(argv: list[str], *, stdout: BinaryIO, timeout: float) -> None:
    with tempfile.TemporaryFile() as errors:
        try:
            result = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=errors,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ReplacementValidationError(f"Full decode could not complete: {exc}") from exc
        errors.seek(0)
        detail = errors.read(4096).decode("utf-8", errors="replace")
        # ffprobe can return zero despite decode errors or a truncated container.
        if result.returncode or detail.strip():
            raise ReplacementValidationError(f"Full decode failed: {detail or result.returncode}")


def _frames(
    path: Path,
    output: BinaryIO,
    *,
    timeout: float,
    stream: str = "v:0",
) -> None:
    _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-err_detect",
            "explode",
            "-select_streams",
            stream,
            "-show_frames",
            "-show_entries",
            "frame=best_effort_timestamp_time",
            "-of",
            "compact=p=0:nk=0",
            str(path),
        ],
        stdout=output,
        timeout=timeout,
    )
    output.seek(0)


def _timestamps(stream: BinaryIO) -> Iterator[float]:
    previous: float | None = None
    for raw in stream:
        fields = dict(part.split("=", 1) for part in raw.decode().strip().split("|") if "=" in part)
        if not fields:
            continue
        timestamp = fields.get("best_effort_timestamp_time")
        if timestamp is None:
            raise ReplacementValidationError("Decoded frame has no timestamp.")
        try:
            value = float(timestamp)
        except ValueError as exc:
            raise ReplacementValidationError("Decoded frame has an invalid timestamp.") from exc
        if not math.isfinite(value) or (previous is not None and value <= previous):
            raise ReplacementValidationError(
                "Decoded frame timestamps are not strictly increasing."
            )
        previous = value
        yield value


def _audio_coverage(path: Path, stream: str, *, timeout: float) -> AudioCoverage:
    with tempfile.TemporaryFile() as frames:
        _run(
            [
                "ffprobe",
                "-v",
                "error",
                "-err_detect",
                "explode",
                "-select_streams",
                stream,
                "-show_frames",
                "-show_entries",
                "frame=best_effort_timestamp_time,pkt_duration_time,duration_time",
                "-of",
                "compact=p=0:nk=0",
                str(path),
            ],
            stdout=frames,
            timeout=timeout,
        )
        frames.seek(0)
        first = previous = end = None
        count = 0
        decoded_seconds = 0.0
        for raw in frames:
            fields = dict(
                part.split("=", 1) for part in raw.decode().strip().split("|") if "=" in part
            )
            if not fields:
                continue
            try:
                timestamp = float(fields["best_effort_timestamp_time"])
                duration = float(
                    fields.get("duration_time", fields.get("pkt_duration_time", "nan"))
                )
            except (KeyError, ValueError) as exc:
                raise ReplacementValidationError("Decoded audio has invalid timing.") from exc
            if (
                not math.isfinite(timestamp)
                or not math.isfinite(duration)
                or duration <= 0
                or (previous is not None and timestamp <= previous)
            ):
                raise ReplacementValidationError("Decoded audio has invalid timing.")
            if first is None:
                first = timestamp
            previous, end = timestamp, timestamp + duration
            decoded_seconds += duration
            count += 1
        if first is None or end is None:
            raise ReplacementValidationError("Required audio stream has no decoded frames.")
        return AudioCoverage(first, end, decoded_seconds, count)


def _compare_audio(
    source: Path,
    output: Path,
    *,
    source_stream: str,
    source_video_start: float,
    output_video_start: float,
    timeout: float,
) -> dict[str, object]:
    left = _audio_coverage(source, source_stream, timeout=timeout)
    right = _audio_coverage(output, "a:0", timeout=timeout)
    if not audio_coverage_matches(
        left,
        right,
        source_video_start=source_video_start,
        output_video_start=output_video_start,
    ):
        raise ReplacementValidationError("Decoded audio timestamp coverage differs.")
    return {
        "source_decoded_seconds": left.decoded_seconds,
        "output_decoded_seconds": right.decoded_seconds,
        "source_frames": left.frames,
        "output_frames": right.frames,
    }


def validate_replacement_media(
    source: Path,
    output: Path,
    *,
    duration_seconds: float,
    source_video_index: int | None = None,
    source_audio_index: int | None = None,
) -> dict[str, object]:
    """Decode both files and compare every video frame's relative presentation time.

    Exact frame counts deliberately fail closed for frame-rate-changing workflows;
    such transformations need an explicit future coverage policy, not a bypass.
    Temporary files bound memory use for multi-hour media.
    """
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise ReplacementValidationError("Replacement needs a finite positive source duration.")
    timeout = max(120.0, duration_seconds * 30)
    with tempfile.TemporaryFile() as sink:
        for path in (source, output):
            _run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-xerror",
                    "-err_detect",
                    "explode",
                    "-i",
                    str(path),
                    "-map",
                    (
                        f"0:{source_video_index}"
                        if path == source and source_video_index is not None
                        else "0:v:0"
                    ),
                    "-map",
                    "0:a?",
                    "-sn",
                    "-dn",
                    "-max_error_rate",
                    "0",
                    "-fps_mode",
                    "passthrough",
                    "-f",
                    "null",
                    "-",
                ],
                stdout=sink,
                timeout=timeout,
            )
    with tempfile.TemporaryFile() as source_frames, tempfile.TemporaryFile() as output_frames:
        _frames(
            source,
            source_frames,
            timeout=timeout,
            stream=str(source_video_index) if source_video_index is not None else "v:0",
        )
        _frames(output, output_frames, timeout=timeout)
        source_times, output_times = _timestamps(source_frames), _timestamps(output_frames)
        source_start = output_start = None
        count = 0
        max_delta = 0.0
        source_span = output_span = 0.0
        while True:
            left, right = next(source_times, None), next(output_times, None)
            if left is None and right is None:
                break
            if left is None or right is None:
                raise ReplacementValidationError("Decoded video frame counts differ.")
            if source_start is None:
                source_start, output_start = left, right
            assert output_start is not None
            source_span, output_span = left - source_start, right - output_start
            delta = abs(source_span - output_span)
            max_delta = max(max_delta, delta)
            if delta > 0.05:
                raise ReplacementValidationError("Decoded video timestamp coverage differs.")
            count += 1
        if count == 0:
            raise ReplacementValidationError("No video frames decoded.")
        frame_interval = source_span / (count - 1) if count > 1 else 0.0
        if abs(source_span + frame_interval - duration_seconds) > max(0.25, frame_interval * 2):
            raise ReplacementValidationError("Decoded source does not cover the planned duration.")
        audio_evidence = None
        if source_audio_index is not None:
            assert source_start is not None and output_start is not None
            audio_evidence = _compare_audio(
                source,
                output,
                source_stream=str(source_audio_index),
                source_video_start=source_start,
                output_video_start=output_start,
                timeout=timeout,
            )
        return {
            "version": 1,
            "audio_coverage": audio_evidence,
            "full_decode": True,
            "decoded_video_frames": count,
            "source_span_seconds": source_span,
            "output_span_seconds": output_span,
            "max_timestamp_delta_seconds": max_delta,
        }
