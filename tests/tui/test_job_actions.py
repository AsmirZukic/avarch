from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from avarch.tui.backend import (
    JobActionRequest,
    JobActionResult,
    SchedulerControlRequest,
    SchedulerControlResult,
)
from avarch.tui.modals.priority import PriorityPrompt
from avarch.tui.models.common import UiRevision
from avarch.tui.models.dashboard import QueueTotals, SchedulerSummary
from avarch.tui.models.queue import QueueFilters, QueueJobRow, QueueSnapshot
from avarch.tui.screens.queue import QueueView


def test_pending_job_offers_hold_and_cancel() -> None:
    async def run() -> None:
        view = _view_with_row(_row(status="PENDING", stage="probe"))
        view.table.selected_job_ids.add(1)

        actions = view.available_actions_for_selected()

        assert "hold" in actions
        assert "cancel" in actions
        assert "priority" in actions

    asyncio.run(run())


def test_held_job_offers_release_and_cancel() -> None:
    async def run() -> None:
        view = _view_with_row(_row(status="HELD", stage="encode"))
        view.table.selected_job_ids.add(1)

        actions = view.available_actions_for_selected()

        assert "release" in actions
        assert "cancel" in actions
        assert "priority" in actions

    asyncio.run(run())


def test_failed_job_offers_retry_and_cancel() -> None:
    async def run() -> None:
        view = _view_with_row(_row(status="FAILED", stage="validate"))
        view.table.selected_job_ids.add(1)

        actions = view.available_actions_for_selected()

        assert "retry" in actions
        assert "cancel" in actions

    asyncio.run(run())


def test_running_job_cancel_explains_request_semantics() -> None:
    async def run() -> None:
        view = _view_with_row(_row(status="RUNNING", stage="encode"))
        view.table.selected_job_ids.add(1)

        prompt = view.prepare_job_action("cancel")

        assert prompt is not None
        assert "requests the running worker to stop" in prompt.content_text

    asyncio.run(run())


def test_completed_job_has_no_cancel_action() -> None:
    async def run() -> None:
        view = _view_with_row(_row(status="COMPLETED", stage="promote"))
        view.table.selected_job_ids.add(1)

        assert "cancel" not in view.available_actions_for_selected()

    asyncio.run(run())


def test_active_promotion_has_no_scheduler_cancel_action() -> None:
    async def run() -> None:
        view = _view_with_row(_row(status="RUNNING", stage="promote"))
        view.table.selected_job_ids.add(1)

        actions = view.available_actions_for_selected()

        assert "cancel" not in actions
        assert "hold" not in actions

    asyncio.run(run())


def test_priority_modal_shows_current_value() -> None:
    async def run() -> None:
        prompt = PriorityPrompt(current_priority=17, job_count=2)

        assert "Current priority: 17" in prompt.content_text
        assert "Selected: 2 jobs" in prompt.content_text

    asyncio.run(run())


def test_action_state_conflict_refreshes_row() -> None:
    async def run() -> None:
        refreshed_snapshot = _snapshot((_row(status="HELD", stage="encode"),))
        backend = FakeJobActionBackend(
            _snapshot((_row(status="PENDING", stage="encode"),)),
            refreshed_snapshot=refreshed_snapshot,
            action_error=RuntimeError("Job 1 cannot be canceled from completed."),
        )
        view = QueueView(backend=backend, snapshot=backend.snapshot)
        view.table.selected_job_ids.add(1)

        result = await view.perform_selected_job_action("cancel", reason="changed")

        assert result is None
        assert backend.action_requests == [
            JobActionRequest(job_ids=(1,), action="cancel", reason="changed")
        ]
        assert backend.refresh_calls == [QueueFilters()]
        assert view.snapshot.rows[0].status == "HELD"
        assert view.error_message == "Job 1 cannot be canceled from completed."

    asyncio.run(run())


class FakeJobActionBackend:
    def __init__(
        self,
        snapshot: QueueSnapshot,
        *,
        refreshed_snapshot: QueueSnapshot | None = None,
        action_error: Exception | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.refreshed_snapshot = refreshed_snapshot or snapshot
        self.action_error = action_error
        self.action_requests: list[JobActionRequest] = []
        self.refresh_calls: list[QueueFilters] = []

    async def get_queue_snapshot(self, filters: QueueFilters) -> QueueSnapshot:
        self.refresh_calls.append(filters)
        return self.refreshed_snapshot

    async def request_scheduler_control(
        self,
        request: SchedulerControlRequest,
    ) -> SchedulerControlResult:
        return SchedulerControlResult(message=f"{request.action} requested")

    async def perform_job_action(self, request: JobActionRequest) -> JobActionResult:
        self.action_requests.append(request)
        if self.action_error is not None:
            raise self.action_error
        return JobActionResult(message=f"{request.action} applied", changed=len(request.job_ids))


def _view_with_row(row: QueueJobRow) -> QueueView:
    snapshot = _snapshot((row,))
    return QueueView(backend=FakeJobActionBackend(snapshot), snapshot=snapshot)


def _snapshot(rows: tuple[QueueJobRow, ...]) -> QueueSnapshot:
    return QueueSnapshot(
        revision=UiRevision(
            scheduler_generation=1,
            newest_job_updated_at=None,
            newest_attempt_updated_at=None,
            newest_validation_created_at=None,
            newest_promotion_updated_at=None,
        ),
        scheduler=SchedulerSummary(
            mode="running",
            lease_state="active",
            runner_id="runner-1",
            heartbeat_at=None,
            lease_expires_at=None,
            control_generation=1,
            acknowledged_generation=1,
            cancel_pending=0,
            hold_pending=0,
        ),
        totals=QueueTotals(pending=1),
        filters=QueueFilters(),
        rows=rows,
    )


def _row(
    *,
    status: str,
    stage: str,
    job_id: int = 1,
    priority: int = 0,
) -> QueueJobRow:
    return QueueJobRow(
        job_id=job_id,
        status=status,
        stage=stage,
        priority=priority,
        attempts=0,
        progress=stage,
        profile_name="av1_1080p",
        file_name="Movie.mkv",
        source_path="/media/Movie.mkv",
        updated_at=datetime.now(UTC),
    )
