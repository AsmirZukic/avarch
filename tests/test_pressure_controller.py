from __future__ import annotations

from avarch.application.pressure_controller import (
    PressureSignalWindow,
    RuntimePressureState,
    classify_runtime_pressure,
)


def test_memory_oom_escalates_to_critical() -> None:
    decision = classify_runtime_pressure(
        PressureSignalWindow(
            memory_used_bytes=1024,
            memory_limit_bytes=4096,
            memory_oom_kill_events_delta=1,
        )
    )

    assert decision.state is RuntimePressureState.CRITICAL
    assert decision.reason == "memory_oom_event"
    assert decision.scale_up_allowed is False


def test_low_memory_reserve_warns_before_cpu_throttling() -> None:
    decision = classify_runtime_pressure(
        PressureSignalWindow(
            memory_used_bytes=900,
            memory_limit_bytes=1000,
            memory_reserve_bytes=0,
            cpu_throttled_events_delta=4,
        )
    )

    assert decision.state is RuntimePressureState.WARNING
    assert decision.reason == "memory_reserve_low"


def test_hysteresis_recovers_before_stable() -> None:
    recovering = classify_runtime_pressure(
        PressureSignalWindow(memory_used_bytes=100, memory_limit_bytes=1000),
        previous_state=RuntimePressureState.CRITICAL,
    )
    stable = classify_runtime_pressure(
        PressureSignalWindow(memory_used_bytes=100, memory_limit_bytes=1000),
        previous_state=recovering.state,
    )

    assert recovering.state is RuntimePressureState.RECOVERING
    assert recovering.scale_up_allowed is False
    assert stable.state is RuntimePressureState.STABLE


def test_missing_signals_cannot_confidently_scale_up() -> None:
    decision = classify_runtime_pressure(PressureSignalWindow())

    assert decision.state is RuntimePressureState.NORMAL
    assert decision.confidence < 0.6
    assert decision.scale_up_allowed is False
