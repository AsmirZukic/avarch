"""Decoded audio coverage summaries independent of codecs and packet sizes."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class AudioCoverage:
    first_timestamp: float
    end_timestamp: float
    decoded_seconds: float
    frames: int


def audio_coverage_matches(
    source: AudioCoverage,
    output: AudioCoverage,
    *,
    source_video_start: float,
    output_video_start: float,
    tolerance: float = 0.1,
) -> bool:
    values = (
        source.first_timestamp,
        source.end_timestamp,
        source.decoded_seconds,
        output.first_timestamp,
        output.end_timestamp,
        output.decoded_seconds,
    )
    if source.frames <= 0 or output.frames <= 0 or not all(map(math.isfinite, values)):
        return False
    return (
        abs(
            (source.first_timestamp - source_video_start)
            - (output.first_timestamp - output_video_start)
        )
        <= tolerance
        and abs(
            (source.end_timestamp - source_video_start)
            - (output.end_timestamp - output_video_start)
        )
        <= tolerance
        and abs(source.decoded_seconds - output.decoded_seconds) <= tolerance
    )
