from datetime import UTC, datetime, timedelta

import pytest

from avarch.models.execution import (
    ProcessFailureReason,
    ProcessResourceSummary,
    ProcessResult,
    ProcessTerminationReason,
)


def test_process_result_records_successful_exit() -> None:
    started_at = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)
    finished_at = started_at + timedelta(seconds=2)

    result = ProcessResult.exited(
        command=("av1an", "--version"),
        return_code=0,
        started_at=started_at,
        finished_at=finished_at,
    )

    assert result.command == ("av1an", "--version")
    assert result.return_code == 0
    assert result.started_at == started_at
    assert result.finished_at == finished_at
    assert result.termination_reason is ProcessTerminationReason.EXITED
    assert result.succeeded is True


def test_process_result_records_nonzero_exit_without_treating_it_as_cancelled() -> None:
    started_at = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)
    finished_at = started_at + timedelta(seconds=2)

    result = ProcessResult.exited(
        command=("ffmpeg", "-version"),
        return_code=2,
        started_at=started_at,
        finished_at=finished_at,
    )

    assert result.return_code == 2
    assert result.termination_reason is ProcessTerminationReason.EXITED
    assert result.succeeded is False


def test_process_result_distinguishes_cancellation_and_forced_kill() -> None:
    started_at = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)
    termination_requested_at = started_at + timedelta(seconds=1)
    finished_at = started_at + timedelta(seconds=2)

    cancelled = ProcessResult.cancelled(
        command=("av1an", "-i", "movie.vpy"),
        return_code=-15,
        started_at=started_at,
        finished_at=finished_at,
        termination_requested_at=termination_requested_at,
    )
    killed = ProcessResult.killed(
        command=("av1an", "-i", "movie.vpy"),
        return_code=-9,
        started_at=started_at,
        finished_at=finished_at,
        termination_requested_at=termination_requested_at,
    )

    assert cancelled.termination_reason is ProcessTerminationReason.CANCELLED
    assert killed.termination_reason is ProcessTerminationReason.FORCED_KILL
    assert cancelled.termination_requested_at == termination_requested_at
    assert killed.termination_requested_at == termination_requested_at
    assert cancelled.succeeded is False
    assert killed.succeeded is False


def test_process_result_classifies_cgroup_oom_kill() -> None:
    started_at = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)
    finished_at = started_at + timedelta(seconds=2)

    result = ProcessResult.exited(
        command=("av1an",),
        return_code=-9,
        started_at=started_at,
        finished_at=finished_at,
        resource_summary=ProcessResourceSummary(memory_oom_kill_events_delta=1),
    )

    assert result.failure_reason is ProcessFailureReason.RESOURCE_OOM
    assert result.resource_summary is not None
    assert result.resource_summary.memory_oom_kill_events_delta == 1


def test_cancelled_process_is_not_reclassified_as_oom() -> None:
    started_at = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)
    termination_requested_at = started_at + timedelta(seconds=1)
    finished_at = started_at + timedelta(seconds=2)

    result = ProcessResult.cancelled(
        command=("av1an",),
        return_code=-9,
        started_at=started_at,
        finished_at=finished_at,
        termination_requested_at=termination_requested_at,
        resource_summary=ProcessResourceSummary(memory_oom_kill_events_delta=1),
    )

    assert result.failure_reason is ProcessFailureReason.CANCELLED


def test_process_result_rejects_termination_request_outside_lifecycle() -> None:
    started_at = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)
    finished_at = started_at + timedelta(seconds=2)

    with pytest.raises(
        ValueError,
        match="termination_requested_at must not be before started_at",
    ):
        ProcessResult.cancelled(
            command=("av1an",),
            return_code=-15,
            started_at=started_at,
            finished_at=finished_at,
            termination_requested_at=started_at - timedelta(microseconds=1),
        )

    with pytest.raises(
        ValueError,
        match="finished_at must not be before termination_requested_at",
    ):
        ProcessResult.killed(
            command=("av1an",),
            return_code=-9,
            started_at=started_at,
            finished_at=finished_at,
            termination_requested_at=finished_at + timedelta(microseconds=1),
        )


def test_process_result_rejects_finish_before_start() -> None:
    started_at = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)

    with pytest.raises(ValueError, match="finished_at must not be before started_at"):
        ProcessResult.exited(
            command=("av1an",),
            return_code=0,
            started_at=started_at,
            finished_at=started_at - timedelta(seconds=1),
        )
