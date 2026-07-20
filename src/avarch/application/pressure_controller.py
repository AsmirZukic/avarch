from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RuntimePressureState(StrEnum):
    NORMAL = "normal"
    WARNING = "warning"
    CRITICAL = "critical"
    RECOVERING = "recovering"
    STABLE = "stable"


@dataclass(frozen=True, slots=True)
class PressureSignalWindow:
    memory_used_bytes: int | None = None
    memory_limit_bytes: int | None = None
    memory_reserve_bytes: int = 0
    swap_current_bytes_delta: int | None = None
    memory_oom_events_delta: int | None = None
    memory_oom_kill_events_delta: int | None = None
    cpu_throttled_events_delta: int | None = None
    samples_observed: int = 1


@dataclass(frozen=True, slots=True)
class PressureDecision:
    state: RuntimePressureState
    reason: str
    confidence: float
    scale_up_allowed: bool


def classify_runtime_pressure(
    window: PressureSignalWindow,
    *,
    previous_state: RuntimePressureState = RuntimePressureState.NORMAL,
    warning_fraction: float = 0.85,
    critical_fraction: float = 0.97,
) -> PressureDecision:
    observed = _instant_pressure(
        window,
        warning_fraction=warning_fraction,
        critical_fraction=critical_fraction,
    )
    if observed.state in {RuntimePressureState.WARNING, RuntimePressureState.CRITICAL}:
        return observed
    if previous_state in {RuntimePressureState.WARNING, RuntimePressureState.CRITICAL}:
        return PressureDecision(
            state=RuntimePressureState.RECOVERING,
            reason="pressure_cleared_waiting_for_stability",
            confidence=observed.confidence,
            scale_up_allowed=False,
        )
    if previous_state is RuntimePressureState.RECOVERING:
        return PressureDecision(
            state=RuntimePressureState.STABLE,
            reason="pressure_stable_after_recovery",
            confidence=observed.confidence,
            scale_up_allowed=observed.confidence >= 0.6,
        )
    return observed


def _instant_pressure(
    window: PressureSignalWindow,
    *,
    warning_fraction: float,
    critical_fraction: float,
) -> PressureDecision:
    if (window.memory_oom_kill_events_delta or 0) > 0 or (
        window.memory_oom_events_delta or 0
    ) > 0:
        return PressureDecision(
            state=RuntimePressureState.CRITICAL,
            reason="memory_oom_event",
            confidence=1.0,
            scale_up_allowed=False,
        )
    if (window.swap_current_bytes_delta or 0) > 0:
        return PressureDecision(
            state=RuntimePressureState.CRITICAL,
            reason="swap_growth",
            confidence=0.9,
            scale_up_allowed=False,
        )
    if window.memory_used_bytes is not None and window.memory_limit_bytes is not None:
        usable = max(1, window.memory_limit_bytes - max(0, window.memory_reserve_bytes))
        fraction = window.memory_used_bytes / usable
        if fraction >= critical_fraction:
            return PressureDecision(
                state=RuntimePressureState.CRITICAL,
                reason="memory_reserve_exhausted",
                confidence=0.9,
                scale_up_allowed=False,
            )
        if fraction >= warning_fraction:
            return PressureDecision(
                state=RuntimePressureState.WARNING,
                reason="memory_reserve_low",
                confidence=0.8,
                scale_up_allowed=False,
            )
        return PressureDecision(
            state=RuntimePressureState.NORMAL,
            reason="within_memory_reserve",
            confidence=0.8 if window.samples_observed > 0 else 0.4,
            scale_up_allowed=True,
        )
    if (window.cpu_throttled_events_delta or 0) > 0:
        return PressureDecision(
            state=RuntimePressureState.WARNING,
            reason="cpu_throttling",
            confidence=0.6,
            scale_up_allowed=False,
        )
    return PressureDecision(
        state=RuntimePressureState.NORMAL,
        reason="insufficient_pressure_signals",
        confidence=0.2,
        scale_up_allowed=False,
    )
