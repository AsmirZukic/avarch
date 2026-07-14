from __future__ import annotations

from typing import Protocol

from avarch.domain.progress import ProgressSnapshot

__all__ = [
    "NoopProgressSink",
    "ProgressSink",
    "RecordingProgressSink",
    "publish_progress_safely",
]


class ProgressSink(Protocol):
    def publish(self, snapshot: ProgressSnapshot) -> None: ...


class NoopProgressSink:
    def publish(self, snapshot: ProgressSnapshot) -> None:
        del snapshot


class RecordingProgressSink:
    def __init__(self) -> None:
        self._snapshots: list[ProgressSnapshot] = []

    @property
    def snapshots(self) -> tuple[ProgressSnapshot, ...]:
        return tuple(self._snapshots)

    def publish(self, snapshot: ProgressSnapshot) -> None:
        self._snapshots.append(snapshot)


def publish_progress_safely(
    sink: ProgressSink | None,
    snapshot: ProgressSnapshot,
) -> bool:
    if sink is None:
        return True
    try:
        sink.publish(snapshot)
    except Exception:
        return False
    return True
