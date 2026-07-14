from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta

from avarch.application.progress import CoalescingProgressBridge
from avarch.domain.progress import ProgressPhase, ProgressSnapshot, ProgressSource, ProgressUnit


def test_bridge_accepts_worker_thread_publication() -> None:
    async def scenario() -> list[ProgressSnapshot]:
        consumed: list[ProgressSnapshot] = []

        async def consume(snapshot: ProgressSnapshot) -> None:
            consumed.append(snapshot)

        bridge = CoalescingProgressBridge(consume)
        thread = threading.Thread(target=lambda: bridge.publish(_snapshot(current=1)))
        thread.start()
        thread.join()

        await bridge.aclose()
        return consumed

    assert asyncio.run(scenario()) == [_snapshot(current=1)]


def test_bridge_coalesces_many_ordinary_snapshots_to_latest() -> None:
    async def scenario() -> list[ProgressSnapshot]:
        consumed: list[ProgressSnapshot] = []

        async def consume(snapshot: ProgressSnapshot) -> None:
            consumed.append(snapshot)

        bridge = CoalescingProgressBridge(consume)
        for value in range(1_000):
            bridge.publish(_snapshot(current=value))

        await bridge.aclose()
        return consumed

    consumed = asyncio.run(scenario())

    assert len(consumed) <= 2
    assert consumed[-1].current == 999


def test_bridge_preserves_phase_transitions() -> None:
    async def scenario() -> list[ProgressPhase]:
        phases: list[ProgressPhase] = []

        async def consume(snapshot: ProgressSnapshot) -> None:
            phases.append(snapshot.phase)

        bridge = CoalescingProgressBridge(consume)
        bridge.publish(_snapshot(phase=ProgressPhase.PREPARING, current=None, unit=None))
        bridge.publish(_snapshot(phase=ProgressPhase.ENCODING, current=1))
        bridge.publish(_snapshot(phase=ProgressPhase.MUXING, current=None, unit=None))

        await bridge.aclose()
        return phases

    assert asyncio.run(scenario()) == [
        ProgressPhase.PREPARING,
        ProgressPhase.ENCODING,
        ProgressPhase.MUXING,
    ]


def test_bridge_preserves_terminal_snapshots() -> None:
    async def scenario() -> list[ProgressPhase]:
        phases: list[ProgressPhase] = []

        async def consume(snapshot: ProgressSnapshot) -> None:
            phases.append(snapshot.phase)

        bridge = CoalescingProgressBridge(consume)
        for value in range(100):
            bridge.publish(_snapshot(current=value))
        bridge.publish(_snapshot(phase=ProgressPhase.COMPLETED, current=100))

        await bridge.aclose()
        return phases

    phases = asyncio.run(scenario())

    assert phases[-1] == ProgressPhase.COMPLETED
    assert ProgressPhase.COMPLETED in phases


def test_bridge_shutdown_flushes_latest_snapshot() -> None:
    async def scenario() -> list[ProgressSnapshot]:
        consumed: list[ProgressSnapshot] = []

        async def consume(snapshot: ProgressSnapshot) -> None:
            consumed.append(snapshot)

        bridge = CoalescingProgressBridge(consume)
        for value in range(250):
            bridge.publish(_snapshot(current=value))

        await bridge.aclose()
        return consumed

    consumed = asyncio.run(scenario())

    assert consumed[-1].current == 249


def test_bridge_publication_after_shutdown_is_safe() -> None:
    async def scenario() -> list[ProgressSnapshot]:
        consumed: list[ProgressSnapshot] = []

        async def consume(snapshot: ProgressSnapshot) -> None:
            consumed.append(snapshot)

        bridge = CoalescingProgressBridge(consume)
        bridge.publish(_snapshot(current=1))
        await bridge.aclose()
        bridge.publish(_snapshot(current=2))
        await asyncio.sleep(0)
        return consumed

    consumed = asyncio.run(scenario())

    assert [snapshot.current for snapshot in consumed] == [1]


def _snapshot(
    *,
    phase: ProgressPhase = ProgressPhase.ENCODING,
    current: int | None = 1,
    unit: ProgressUnit | None = ProgressUnit.FRAMES,
) -> ProgressSnapshot:
    now = datetime(2026, 7, 1, 12, tzinfo=UTC)
    if current is not None:
        now += timedelta(seconds=current)
    return ProgressSnapshot(
        phase=phase,
        current=current,
        total=1_000 if current is not None else None,
        unit=unit,
        rate_per_second=None,
        speed_ratio=None,
        source=ProgressSource.SCHEDULER,
        message=None,
        phase_started_at=datetime(2026, 7, 1, 12, tzinfo=UTC),
        observed_at=now,
        heartbeat_at=now,
        advanced_at=now if current is not None else None,
    )
