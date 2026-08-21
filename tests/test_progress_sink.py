from datetime import UTC, datetime

from avarch.application.progress import publish_progress_safely
from avarch.domain.progress import ProgressPhase, ProgressSnapshot, ProgressSource


def test_publish_progress_safely_swallows_sink_failure() -> None:
    class BrokenSink:
        def publish(self, snapshot: ProgressSnapshot) -> None:
            del snapshot
            raise RuntimeError("observer failed")

    assert publish_progress_safely(BrokenSink(), _snapshot()) is False


def test_publish_progress_safely_accepts_missing_sink() -> None:
    assert publish_progress_safely(None, _snapshot()) is True


def _snapshot() -> ProgressSnapshot:
    now = datetime(2026, 7, 1, 12, tzinfo=UTC)
    return ProgressSnapshot(
        phase=ProgressPhase.PREPARING,
        current=None,
        total=None,
        unit=None,
        rate_per_second=None,
        speed_ratio=None,
        source=ProgressSource.SCHEDULER,
        message=None,
        phase_started_at=now,
        observed_at=now,
        heartbeat_at=now,
        advanced_at=None,
    )
