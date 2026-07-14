from __future__ import annotations

from enum import StrEnum


class ProgressPhase(StrEnum):
    PREPARING = "preparing"
    SCENE_DETECTION = "scene_detection"
    ENCODING = "encoding"
    CONCATENATING = "concatenating"
    MUXING = "muxing"
    VALIDATING = "validating"
    PROMOTING = "promoting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProgressUnit(StrEnum):
    FRAMES = "frames"
    CHUNKS = "chunks"
    SECONDS = "seconds"
    BYTES = "bytes"
    UNKNOWN = "unknown"


class ProgressSource(StrEnum):
    AV1AN_STRUCTURED = "av1an_structured"
    AV1AN_STATE = "av1an_state"
    AV1AN_OUTPUT = "av1an_output"
    FFMPEG_PROGRESS = "ffmpeg_progress"
    SCHEDULER = "scheduler"
    PROCESS_HEARTBEAT = "process_heartbeat"
