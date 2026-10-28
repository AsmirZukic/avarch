from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FfmpegProgressSample:
    frame: int | None
    fps: float | None
    speed_ratio: float | None
    bitrate_kbps: int | None
    encoded_seconds: float | None
    written_bytes: int | None
    completed: bool


class FfmpegProgressParser:
    """Incrementally parse FFmpeg's machine-readable ``-progress`` records."""

    def __init__(self) -> None:
        self._buffer = ""
        self._record: dict[str, str] = {}

    def feed(self, data: bytes) -> tuple[FfmpegProgressSample, ...]:
        self._buffer += data.decode("utf-8", errors="replace")
        lines = self._buffer.split("\n")
        self._buffer = lines.pop()
        samples: list[FfmpegProgressSample] = []
        for line in lines:
            sample = self._consume_line(line.rstrip("\r"))
            if sample is not None:
                samples.append(sample)
        return tuple(samples)

    def flush(self) -> tuple[FfmpegProgressSample, ...]:
        samples: list[FfmpegProgressSample] = []
        if self._buffer:
            sample = self._consume_line(self._buffer.rstrip("\r"))
            if sample is not None:
                samples.append(sample)
            self._buffer = ""
        return tuple(samples)

    def _consume_line(self, line: str) -> FfmpegProgressSample | None:
        if "=" not in line:
            return None
        key, value = line.split("=", 1)
        self._record[key.strip()] = value.strip()
        if key.strip() != "progress":
            return None
        sample = _sample_from_record(self._record)
        self._record = {}
        return sample


def _sample_from_record(record: dict[str, str]) -> FfmpegProgressSample:
    return FfmpegProgressSample(
        frame=_optional_int(record.get("frame")),
        fps=_optional_float(record.get("fps")),
        speed_ratio=_speed_ratio(record.get("speed")),
        bitrate_kbps=_bitrate_kbps(record.get("bitrate")),
        encoded_seconds=_encoded_seconds(record),
        written_bytes=_optional_int(record.get("total_size")),
        completed=record.get("progress") == "end",
    )


def _encoded_seconds(record: dict[str, str]) -> float | None:
    microseconds = _optional_int(record.get("out_time_us"))
    if microseconds is None:
        microseconds = _optional_int(record.get("out_time_ms"))
    if microseconds is not None:
        return max(0.0, microseconds / 1_000_000.0)
    value = record.get("out_time")
    if not value:
        return None
    parts = value.split(":")
    if len(parts) != 3:
        return None
    try:
        hours, minutes, seconds = (float(part) for part in parts)
    except ValueError:
        return None
    return max(0.0, (hours * 3600.0) + (minutes * 60.0) + seconds)


def _speed_ratio(value: str | None) -> float | None:
    if value is None:
        return None
    return _optional_float(value.removesuffix("x"))


def _bitrate_kbps(value: str | None) -> int | None:
    if value is None or value == "N/A":
        return None
    normalized = value.lower().removesuffix("kbits/s").strip()
    parsed = _optional_float(normalized)
    return None if parsed is None else max(0, round(parsed))


def _optional_int(value: str | None) -> int | None:
    if value is None or value == "N/A":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _optional_float(value: str | None) -> float | None:
    if value is None or value == "N/A":
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if parsed >= 0 else None
