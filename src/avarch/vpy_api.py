from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

AVARCH_FILTER_API_VERSION = 1
AVARCH_TEMPLATE_API_VERSION = 1


@dataclass(frozen=True, slots=True)
class FilterContext:
    api_version: int
    source_path: Path
    source_relative_path: Path
    video_stream_index: int
    source_width: int
    source_height: int
    target_width: int
    target_height: int
    source_fps_num: int | None
    source_fps_den: int | None
    source_is_hdr: bool
    color_primaries: str | None
    color_transfer: str | None
    color_matrix: str | None
    work_dir: Path
    cache_dir: Path
