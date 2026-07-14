from __future__ import annotations

import re
from dataclasses import dataclass

from avarch.domain.progress import ProgressPhase, ProgressUnit

SUPPORTED_AV1AN_TTY_PROGRESS_VERSION_FAMILY = "0.5.x"

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_CHUNKS_RE = re.compile(r"\[(?P<current>\d+)/(?P<total>\d+)\s+Chunks\]", re.IGNORECASE)
_PROGRESS_RE = re.compile(
    r".*?\b(?P<current>\d+)/(?P<total>\d+)\s*"
    r"\(\s*(?P<fps>\d+(?:\.\d+)?)\s+fps\b",
    re.IGNORECASE,
)
_INPUT_FPS_RE = re.compile(
    r"\bInput:\s+.+?\s+@\s+(?P<fps>\d+(?:\.\d+)?)\s+fps\b",
    re.IGNORECASE,
)
_BITRATE_RE = re.compile(
    r",\s*(?P<bitrate>\d+(?:\.\d+)?\s+[KMGT]?bps)\b",
    re.IGNORECASE,
)
_ESTIMATED_SIZE_RE = re.compile(
    r",\s*(?P<size>est\.\s+\d+(?:\.\d+)?\s+[KMGT]?i?B)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Av1anTtyProgressSample:
    phase: ProgressPhase
    current: int
    total: int
    unit: ProgressUnit
    rate_per_second: float | None
    speed_ratio: float | None
    message: str | None


def av1an_tty_progress_supported(version_family: str, *, enabled: bool = True) -> bool:
    return enabled and version_family == SUPPORTED_AV1AN_TTY_PROGRESS_VERSION_FAMILY


def parse_av1an_tty_progress(data: bytes) -> list[Av1anTtyProgressSample]:
    text = _normalize_for_parser(data)
    source_fps: float | None = None
    samples: list[Av1anTtyProgressSample] = []
    for record in re.split(r"[\r\n]+", text):
        source_fps = _parse_source_fps(record) or source_fps
        sample = _parse_record(record, source_fps=source_fps)
        if sample is not None:
            samples.append(sample)
    return samples


class Av1anTtyProgressParser:
    def __init__(self) -> None:
        self._buffer = bytearray()
        self._source_fps: float | None = None

    def feed(self, data: bytes) -> list[Av1anTtyProgressSample]:
        self._buffer.extend(data)
        records = self._pop_complete_records()
        samples: list[Av1anTtyProgressSample] = []
        for record in records:
            normalized = _normalize_for_parser(record)
            self._source_fps = _parse_source_fps(normalized) or self._source_fps
            sample = _parse_record(normalized, source_fps=self._source_fps)
            if sample is not None:
                samples.append(sample)
        return samples

    def flush(self) -> list[Av1anTtyProgressSample]:
        if not self._buffer:
            return []
        record = bytes(self._buffer)
        self._buffer.clear()
        normalized = _normalize_for_parser(record)
        self._source_fps = _parse_source_fps(normalized) or self._source_fps
        sample = _parse_record(normalized, source_fps=self._source_fps)
        return [] if sample is None else [sample]

    def _pop_complete_records(self) -> list[bytes]:
        records: list[bytes] = []
        start = 0
        index = 0
        while index < len(self._buffer):
            byte = self._buffer[index]
            if byte not in {10, 13}:
                index += 1
                continue
            records.append(bytes(self._buffer[start:index]))
            index += 1
            if byte == 13 and index < len(self._buffer) and self._buffer[index] == 10:
                index += 1
            start = index
        if start:
            del self._buffer[:start]
        return records


def _parse_record(
    record: str,
    *,
    source_fps: float | None,
) -> Av1anTtyProgressSample | None:
    match = _PROGRESS_RE.search(record)
    if match is None:
        return None
    try:
        current = int(match.group("current"))
        total = int(match.group("total"))
        rate = float(match.group("fps"))
    except ValueError:
        return None
    chunks = _CHUNKS_RE.search(record)
    if chunks is None:
        phase = ProgressPhase.SCENE_DETECTION
        message = None
    else:
        phase = ProgressPhase.ENCODING
        message = _encoding_message(record, chunks=chunks)
    return Av1anTtyProgressSample(
        phase=phase,
        current=current,
        total=total,
        unit=ProgressUnit.FRAMES,
        rate_per_second=rate,
        speed_ratio=_speed_ratio(rate, source_fps=source_fps),
        message=message,
    )


def _parse_source_fps(record: str) -> float | None:
    match = _INPUT_FPS_RE.search(record)
    if match is None:
        return None
    try:
        fps = float(match.group("fps"))
    except ValueError:
        return None
    return fps if fps > 0 else None


def _encoding_message(record: str, *, chunks: re.Match[str]) -> str:
    parts = [f"{int(chunks.group('current'))}/{int(chunks.group('total'))} chunks"]
    bitrate = _BITRATE_RE.search(record)
    if bitrate is not None:
        parts.append(bitrate.group("bitrate"))
    estimated_size = _ESTIMATED_SIZE_RE.search(record)
    if estimated_size is not None:
        parts.append(estimated_size.group("size"))
    return ", ".join(parts)


def _speed_ratio(rate: float, *, source_fps: float | None) -> float | None:
    if rate <= 0 or source_fps is None:
        return None
    return rate / source_fps


def _normalize_for_parser(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    return _ANSI_RE.sub("", text)
