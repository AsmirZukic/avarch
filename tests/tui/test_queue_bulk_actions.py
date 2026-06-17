from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from avarch.tui.backend import (
    JobActionRequest,
    JobActionResult,
    SchedulerControlRequest,
    SchedulerControlResult,
)
from avarch.tui.models.common import UiRevision
from avarch.tui.models.dashboard import QueueTotals, SchedulerSummary
from avarch.tui.models.queue import (
    QueueClearFilters,
    QueueClearPreview,
    QueueClearResult,
    QueueFilters,
    QueueJobRow,
    QueueRetryPreview,
    QueueRetryResult,
    QueueSnapshot,
)
from avarch.tui.screens.queue import QueueView


def test_bulk_clear_requires_selection_or_filter() -> None:
    async def run() -> None:
        backend = FakeBulkBackend(_snapshot((_row(1, "PENDING", "probe"),)))
        view = QueueView(backend=backend, snapshot=backend.snapshot)

        prompt = await view.prepare_bulk_clear()

        assert prompt is None
        assert backend.clear_previews == []
        assert (
            view.error_message
            == "Select jobs or apply a status, stage, or profile filter first."
        )

    asyncio.run(run())


def test_bulk_clear_shows_preview_counts() -> None:
    async def run() -> None:
        backend = FakeBulkBackend(
            _snapshot((_row(1, "PENDING", "probe"),)),
            clear_preview=_clear_preview(matched=3, cancel_immediately=2, request_interruption=1),
        )
        view = QueueView(backend=backend, snapshot=backend.snapshot)
        view.table.selected_job_ids.add(1)

        prompt = await view.prepare_bulk_clear()

        assert prompt is not None
        assert "Matched: 3" in prompt.content_text
        assert "Cancel immediately: 2" in prompt.content_text
        assert "Request running interruption: 1" in prompt.content_text

    asyncio.run(run())


def test_bulk_clear_labels_action_as_cancel() -> None:
    async def run() -> None:
        backend = FakeBulkBackend(_snapshot((_row(1, "PENDING", "probe"),)))
        view = QueueView(backend=backend, snapshot=backend.snapshot)
        view.table.selected_job_ids.add(1)

        prompt = await view.prepare_bulk_clear()

        assert prompt is not None
        assert "Bulk cancel preview" in prompt.content_text
        assert "clear" not in prompt.content_text.lower()

    asyncio.run(run())


def test_bulk_clear_excludes_completed_jobs() -> None:
    async def run() -> None:
        backend = FakeBulkBackend(
            _snapshot((_row(1, "COMPLETED", "promote"),)),
            clear_preview=_clear_preview(matched=1, completed_excluded=1),
        )
        view = QueueView(backend=backend, snapshot=backend.snapshot)
        view.table.selected_job_ids.add(1)

        prompt = await view.prepare_bulk_clear()

        assert prompt is not None
        assert "Completed excluded: 1" in prompt.content_text

    asyncio.run(run())


def test_bulk_clear_excludes_active_promotions() -> None:
    async def run() -> None:
        backend = FakeBulkBackend(
            _snapshot((_row(1, "RUNNING", "promote"),)),
            clear_preview=_clear_preview(matched=1, active_promotions_excluded=1),
        )
        view = QueueView(backend=backend, snapshot=backend.snapshot)
        view.table.selected_job_ids.add(1)

        prompt = await view.prepare_bulk_clear()

        assert prompt is not None
        assert "Active promotions excluded: 1" in prompt.content_text

    asyncio.run(run())


def test_bulk_clear_requires_second_confirmation() -> None:
    async def run() -> None:
        backend = FakeBulkBackend(_snapshot((_row(1, "PENDING", "probe"),)))
        view = QueueView(backend=backend, snapshot=backend.snapshot)
        view.table.selected_job_ids.add(1)
        await view.prepare_bulk_clear()

        first_result = await view.confirm_bulk_clear()
        second_result = await view.confirm_bulk_clear(confirmed=True)

        assert first_result is None
        assert len(backend.clear_confirms) == 1
        assert backend.clear_confirms[0].selected_job_ids == frozenset({1})
        assert second_result == QueueClearResult(changed=1, operation_id="clear-1")

    asyncio.run(run())


def test_bulk_retry_shows_resume_stage_counts() -> None:
    async def run() -> None:
        backend = FakeBulkBackend(
            _snapshot((_row(1, "FAILED", "validate"),)),
            retry_preview=QueueRetryPreview(
                filters=QueueClearFilters(selected_job_ids=frozenset({1})),
                matched=4,
                retryable=3,
                requires_requeue=1,
                reset_to_probe=1,
                reset_to_plan=1,
                reset_to_encode=0,
                reset_to_validate=1,
                return_to_promote=0,
            ),
        )
        view = QueueView(backend=backend, snapshot=backend.snapshot)
        view.table.selected_job_ids.add(1)

        prompt = await view.prepare_bulk_retry()

        assert prompt is not None
        assert "Retryable: 3" in prompt.content_text
        assert "Resume at probe: 1" in prompt.content_text
        assert "Resume at plan: 1" in prompt.content_text
        assert "Resume at validate: 1" in prompt.content_text
        assert "Requires requeue: 1" in prompt.content_text

    asyncio.run(run())


def test_preview_performs_no_mutation() -> None:
    async def run() -> None:
        backend = FakeBulkBackend(_snapshot((_row(1, "PENDING", "probe"),)))
        view = QueueView(backend=backend, snapshot=backend.snapshot)
        view.table.selected_job_ids.add(1)

        await view.prepare_bulk_clear()

        assert backend.clear_previews == [
            QueueClearFilters(selected_job_ids=frozenset({1}))
        ]
        assert backend.clear_confirms == []

    asyncio.run(run())


class FakeBulkBackend:
    def __init__(
        self,
        snapshot: QueueSnapshot,
        *,
        clear_preview: QueueClearPreview | None = None,
        retry_preview: QueueRetryPreview | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.clear_preview = clear_preview or _clear_preview(matched=1, cancel_immediately=1)
        self.retry_preview = retry_preview or QueueRetryPreview(
            filters=QueueClearFilters(selected_job_ids=frozenset({1})),
            matched=1,
            retryable=1,
            requires_requeue=0,
            reset_to_probe=0,
            reset_to_plan=0,
            reset_to_encode=1,
            reset_to_validate=0,
            return_to_promote=0,
        )
        self.clear_previews: list[QueueClearFilters] = []
        self.retry_previews: list[QueueClearFilters] = []
        self.clear_confirms: list[QueueClearFilters] = []
        self.retry_confirms: list[QueueClearFilters] = []

    async def get_queue_snapshot(self, filters: QueueFilters) -> QueueSnapshot:
        return self.snapshot

    async def request_scheduler_control(
        self,
        request: SchedulerControlRequest,
    ) -> SchedulerControlResult:
        return SchedulerControlResult(message=f"{request.action} requested")

    async def perform_job_action(self, request: JobActionRequest) -> JobActionResult:
        return JobActionResult(message=f"{request.action} applied", changed=len(request.job_ids))

    async def preview_queue_clear(self, filters: QueueClearFilters) -> QueueClearPreview:
        self.clear_previews.append(filters)
        return self.clear_preview.with_filters(filters)

    async def confirm_queue_clear(self, preview: QueueClearPreview) -> QueueClearResult:
        self.clear_confirms.append(preview.filters)
        return QueueClearResult(changed=1, operation_id="clear-1")

    async def preview_queue_retry(self, filters: QueueClearFilters) -> QueueRetryPreview:
        self.retry_previews.append(filters)
        return self.retry_preview.with_filters(filters)

    async def confirm_queue_retry(self, preview: QueueRetryPreview) -> QueueRetryResult:
        self.retry_confirms.append(preview.filters)
        return QueueRetryResult(changed=preview.retryable)


def _clear_preview(
    *,
    matched: int,
    cancel_immediately: int = 0,
    request_interruption: int = 0,
    active_promotions_excluded: int = 0,
    completed_excluded: int = 0,
) -> QueueClearPreview:
    return QueueClearPreview(
        filters=QueueClearFilters(selected_job_ids=frozenset({1})),
        matched=matched,
        cancel_immediately=cancel_immediately,
        request_interruption=request_interruption,
        active_promotions_excluded=active_promotions_excluded,
        completed_excluded=completed_excluded,
    )


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


def _row(job_id: int, status: str, stage: str) -> QueueJobRow:
    return QueueJobRow(
        job_id=job_id,
        status=status,
        stage=stage,
        priority=0,
        attempts=0,
        progress=stage,
        profile_name="av1_1080p",
        file_name="Movie.mkv",
        source_path="/media/Movie.mkv",
        updated_at=datetime.now(UTC),
    )
