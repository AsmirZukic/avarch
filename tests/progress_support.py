from avarch.domain.progress import ProgressSnapshot


class RecordingProgressSink:
    def __init__(self) -> None:
        self._snapshots: list[ProgressSnapshot] = []

    @property
    def snapshots(self) -> tuple[ProgressSnapshot, ...]:
        return tuple(self._snapshots)

    def publish(self, snapshot: ProgressSnapshot) -> None:
        self._snapshots.append(snapshot)
