from __future__ import annotations

import re
from dataclasses import dataclass

from avarch.domain.progress import ProgressPhase, ProgressUnit

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_CHUNKS_RE = re.compile(r"\[(?P<current>\d+)/(?P<total>\d+)\s+Chunks\]", re.IGNORECASE)
_PROGRESS_RE = re.compile(
    r".*?\b(?P<current>\d+)/(?P<total>\d+)\s*"
    r"\(\s*(?P<fps>\d+(?:\.\d+)?)\s+fps\b",
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


def parse_av1an_tty_progress(data: bytes) -> list[Av1anTtyProgressSample]:
    text = _normalize_for_parser(data)
    return [
        sample
        for record in re.split(r"[\r\n]+", text)
        if (sample := _parse_record(record)) is not None
    ]


class Av1anTtyProgressParser:
    def __init__(self) -> None:
        self._buffer = bytearray()

    def feed(self, data: bytes) -> list[Av1anTtyProgressSample]:
        self._buffer.extend(data)
        records = self._pop_complete_records()
        return [
            sample
            for record in records
            if (sample := _parse_record(_normalize_for_parser(record))) is not None
        ]

    def flush(self) -> list[Av1anTtyProgressSample]:
        if not self._buffer:
            return []
        record = bytes(self._buffer)
        self._buffer.clear()
        sample = _parse_record(_normalize_for_parser(record))
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


def _parse_record(record: str) -> Av1anTtyProgressSample | None:
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
        message = f"{int(chunks.group('current'))}/{int(chunks.group('total'))} chunks"
    return Av1anTtyProgressSample(
        phase=phase,
        current=current,
        total=total,
        unit=ProgressUnit.FRAMES,
        rate_per_second=rate,
        speed_ratio=None,
        message=message,
    )


def _normalize_for_parser(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    return _ANSI_RE.sub("", text)
