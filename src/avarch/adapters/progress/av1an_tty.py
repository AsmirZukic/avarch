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
