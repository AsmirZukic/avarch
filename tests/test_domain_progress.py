import math
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from avarch.domain.progress import (
    ProgressPhase,
    ProgressSnapshot,
    ProgressSource,
    ProgressUnit,
    derive_progress_timing,
    phase_progress_percent,
)

NOW = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)


def test_progress_phase_values_are_stable_for_serialization() -> None:
    assert [phase.value for phase in ProgressPhase] == [
        "preparing",
        "scene_detection",
        "encoding",
        "muxing",
        "validating",
        "promoting",
        "completed",
        "failed",
        "cancelled",
    ]
    assert ProgressPhase("encoding") is ProgressPhase.ENCODING


def test_progress_units_are_stable_for_serialization() -> None:
    assert [unit.value for unit in ProgressUnit] == [
        "frames",
        "unknown",
    ]
    assert ProgressUnit("frames") is ProgressUnit.FRAMES


def test_progress_sources_are_stable_for_serialization() -> None:
    assert [source.value for source in ProgressSource] == [
        "av1an_structured",
        "av1an_state",
        "av1an_output",
        "ffmpeg_progress",
        "scheduler",
        "process_heartbeat",
    ]
    assert ProgressSource("av1an_output") is ProgressSource.AV1AN_OUTPUT


@pytest.mark.parametrize(
    ("enum_type", "value"),
    [
        (ProgressPhase, "overall"),
        (ProgressUnit, "percent"),
        (ProgressSource, "console_guess"),
    ],
)
def test_progress_enums_reject_invalid_values(enum_type: Any, value: str) -> None:
    with pytest.raises(ValueError):
        enum_type(value)


def test_progress_snapshot_accepts_phase_only_progress() -> None:
    snapshot = ProgressSnapshot(
        phase=ProgressPhase.MUXING,
        current=None,
        total=None,
        unit=None,
        rate_per_second=None,
        speed_ratio=None,
        source=ProgressSource.SCHEDULER,
        message=None,
        phase_started_at=NOW,
        observed_at=NOW,
        heartbeat_at=NOW,
        advanced_at=None,
    )

    assert snapshot.phase is ProgressPhase.MUXING
    assert snapshot.current is None
    assert snapshot.unit is None


def test_progress_snapshot_accepts_numeric_progress_with_rate_and_speed() -> None:
    snapshot = ProgressSnapshot(
        phase=ProgressPhase.ENCODING,
        current=120,
        total=240,
        unit=ProgressUnit.FRAMES,
        rate_per_second=48.5,
        speed_ratio=1.25,
        source=ProgressSource.AV1AN_OUTPUT,
        message="0/1 chunks",
        phase_started_at=NOW,
        observed_at=NOW + timedelta(seconds=2),
        heartbeat_at=NOW + timedelta(seconds=2),
        advanced_at=NOW + timedelta(seconds=2),
    )

    assert snapshot.current == 120
    assert snapshot.total == 240
    assert snapshot.rate_per_second == 48.5
    assert snapshot.speed_ratio == 1.25


def test_progress_snapshot_allows_current_beyond_total_from_source_truth() -> None:
    snapshot = ProgressSnapshot(
        phase=ProgressPhase.ENCODING,
        current=241,
        total=240,
        unit=ProgressUnit.FRAMES,
        rate_per_second=None,
        speed_ratio=None,
        source=ProgressSource.AV1AN_OUTPUT,
        message=None,
        phase_started_at=NOW,
        observed_at=NOW,
        heartbeat_at=NOW,
        advanced_at=NOW,
    )

    assert snapshot.current == 241


@pytest.mark.parametrize(
    "updates",
    [
        {"current": -1, "unit": ProgressUnit.FRAMES},
        {"total": 0, "unit": ProgressUnit.FRAMES},
        {"total": -1, "unit": ProgressUnit.FRAMES},
        {"current": 1, "unit": None},
        {"rate_per_second": math.inf},
        {"rate_per_second": -1.0},
        {"speed_ratio": math.nan},
        {"speed_ratio": -0.1},
        {"observed_at": NOW - timedelta(seconds=1)},
        {"heartbeat_at": NOW - timedelta(seconds=1)},
        {"advanced_at": NOW - timedelta(seconds=1)},
    ],
)
def test_progress_snapshot_rejects_invalid_values(updates: dict[str, object]) -> None:
    values: dict[str, Any] = {
        "phase": ProgressPhase.ENCODING,
        "current": None,
        "total": None,
        "unit": None,
        "rate_per_second": None,
        "speed_ratio": None,
        "source": ProgressSource.AV1AN_OUTPUT,
        "message": None,
        "phase_started_at": NOW,
        "observed_at": NOW,
        "heartbeat_at": NOW,
        "advanced_at": None,
    }
    values.update(updates)

    with pytest.raises(ValueError):
        ProgressSnapshot(**cast(Any, values))


@pytest.mark.parametrize(
    ("current", "total", "expected"),
    [
        (0, 100, 0.0),
        (25, 100, 25.0),
        (100, 100, 100.0),
        (2.5, 10.0, 25.0),
        (101, 100, 100.0),
    ],
)
def test_phase_progress_percent_for_known_progress(
    current: int | float,
    total: int | float,
    expected: float,
) -> None:
    snapshot = ProgressSnapshot(
        phase=ProgressPhase.ENCODING,
        current=current,
        total=total,
        unit=ProgressUnit.FRAMES,
        rate_per_second=None,
        speed_ratio=None,
        source=ProgressSource.AV1AN_OUTPUT,
        message=None,
        phase_started_at=NOW,
        observed_at=NOW,
        heartbeat_at=NOW,
        advanced_at=NOW,
    )

    assert phase_progress_percent(snapshot) == expected


@pytest.mark.parametrize(
    "snapshot",
    [
        ProgressSnapshot(
            phase=ProgressPhase.MUXING,
            current=None,
            total=None,
            unit=None,
            rate_per_second=None,
            speed_ratio=None,
            source=ProgressSource.SCHEDULER,
            message=None,
            phase_started_at=NOW,
            observed_at=NOW,
            heartbeat_at=NOW,
            advanced_at=None,
        ),
        ProgressSnapshot(
            phase=ProgressPhase.ENCODING,
            current=20,
            total=None,
            unit=ProgressUnit.FRAMES,
            rate_per_second=None,
            speed_ratio=None,
            source=ProgressSource.AV1AN_OUTPUT,
            message=None,
            phase_started_at=NOW,
            observed_at=NOW,
            heartbeat_at=NOW,
            advanced_at=NOW,
        ),
    ],
)
def test_phase_progress_percent_returns_none_for_unknown_progress(
    snapshot: ProgressSnapshot,
) -> None:
    assert phase_progress_percent(snapshot) is None


def test_progress_timing_derives_elapsed_and_recent_heartbeat() -> None:
    snapshot = _snapshot(
        phase=ProgressPhase.ENCODING,
        phase_started_at=NOW,
        observed_at=NOW + timedelta(seconds=10),
        heartbeat_at=NOW + timedelta(seconds=9),
        advanced_at=NOW + timedelta(seconds=8),
    )

    timing = derive_progress_timing(
        snapshot,
        now=NOW + timedelta(seconds=12),
        heartbeat_stale_after=timedelta(seconds=5),
        advancement_stale_after=timedelta(seconds=5),
    )

    assert timing.phase_elapsed == timedelta(seconds=12)
    assert timing.heartbeat_age == timedelta(seconds=3)
    assert timing.advance_age == timedelta(seconds=4)
    assert timing.heartbeat_stale is False
    assert timing.not_advancing is False


def test_progress_timing_distinguishes_stale_heartbeat_from_no_advancement() -> None:
    snapshot = _snapshot(
        phase=ProgressPhase.ENCODING,
        phase_started_at=NOW,
        observed_at=NOW + timedelta(seconds=30),
        heartbeat_at=NOW + timedelta(seconds=29),
        advanced_at=NOW + timedelta(seconds=5),
    )

    timing = derive_progress_timing(
        snapshot,
        now=NOW + timedelta(seconds=31),
        heartbeat_stale_after=timedelta(seconds=5),
        advancement_stale_after=timedelta(seconds=10),
    )

    assert timing.heartbeat_stale is False
    assert timing.not_advancing is True


def test_progress_timing_marks_old_heartbeat_stale() -> None:
    snapshot = _snapshot(
        phase=ProgressPhase.ENCODING,
        phase_started_at=NOW,
        observed_at=NOW + timedelta(seconds=10),
        heartbeat_at=NOW + timedelta(seconds=2),
        advanced_at=None,
    )

    timing = derive_progress_timing(
        snapshot,
        now=NOW + timedelta(seconds=12),
        heartbeat_stale_after=timedelta(seconds=5),
        advancement_stale_after=timedelta(seconds=5),
    )

    assert timing.heartbeat_stale is True
    assert timing.advance_age is None
    assert timing.not_advancing is False


def test_progress_timing_does_not_mark_terminal_phase_stale() -> None:
    snapshot = _snapshot(
        phase=ProgressPhase.COMPLETED,
        phase_started_at=NOW,
        observed_at=NOW + timedelta(seconds=20),
        heartbeat_at=NOW + timedelta(seconds=20),
        advanced_at=NOW + timedelta(seconds=3),
    )

    timing = derive_progress_timing(
        snapshot,
        now=NOW + timedelta(minutes=5),
        heartbeat_stale_after=timedelta(seconds=5),
        advancement_stale_after=timedelta(seconds=5),
    )

    assert timing.heartbeat_stale is False
    assert timing.not_advancing is False


def test_progress_timing_clamps_clock_skew_to_zero() -> None:
    snapshot = _snapshot(
        phase=ProgressPhase.ENCODING,
        phase_started_at=NOW,
        observed_at=NOW,
        heartbeat_at=NOW,
        advanced_at=NOW,
    )

    timing = derive_progress_timing(
        snapshot,
        now=NOW - timedelta(seconds=1),
        heartbeat_stale_after=timedelta(seconds=5),
        advancement_stale_after=timedelta(seconds=5),
    )

    assert timing.phase_elapsed == timedelta()
    assert timing.heartbeat_age == timedelta()
    assert timing.advance_age == timedelta()


def _snapshot(
    *,
    phase: ProgressPhase,
    phase_started_at: datetime,
    observed_at: datetime,
    heartbeat_at: datetime,
    advanced_at: datetime | None,
) -> ProgressSnapshot:
    return ProgressSnapshot(
        phase=phase,
        current=None,
        total=None,
        unit=None,
        rate_per_second=None,
        speed_ratio=None,
        source=ProgressSource.SCHEDULER,
        message=None,
        phase_started_at=phase_started_at,
        observed_at=observed_at,
        heartbeat_at=heartbeat_at,
        advanced_at=advanced_at,
    )
