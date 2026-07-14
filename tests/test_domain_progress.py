from typing import Any

import pytest

from avarch.domain.progress import ProgressPhase, ProgressSource, ProgressUnit


def test_progress_phase_values_are_stable_for_serialization() -> None:
    assert [phase.value for phase in ProgressPhase] == [
        "preparing",
        "scene_detection",
        "encoding",
        "concatenating",
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
        "chunks",
        "seconds",
        "bytes",
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
