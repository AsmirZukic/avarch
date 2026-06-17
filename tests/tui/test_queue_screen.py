from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from textual.app import App, ComposeResult

from avarch.models.scheduler import JobStage, JobStatus
from avarch.tui.models.common import UiRevision
from avarch.tui.models.dashboard import QueueTotals, SchedulerSummary
from avarch.tui.models.queue import QueueFilters, QueueJobRow, QueueSnapshot
from avarch.tui.screens.queue import QueueView


def test_queue_shows_status_stage_and_profile() -> None:
    async def run() -> None:
        app = QueueTestApp(_snapshot())
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            text = app.view.table.content_text
            assert "RUNNING" in text
            assert "encode" in text
            assert "my_1080p" in text
            assert "Movie A.mkv" in text

    asyncio.run(run())


def test_queue_filters_status() -> None:
    async def run() -> None:
        backend = FakeQueueBackend(_snapshot())
        app = QueueTestApp(_snapshot(), backend=backend)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await app.view.apply_filters(
                QueueFilters(statuses=frozenset({JobStatus.FAILED}))
            )
            assert backend.calls[-1].statuses == frozenset({JobStatus.FAILED})
            assert "Movie C.mkv" in app.view.table.content_text
            assert "Movie A.mkv" not in app.view.table.content_text

    asyncio.run(run())


def test_queue_filters_stage() -> None:
    async def run() -> None:
        backend = FakeQueueBackend(_snapshot())
        app = QueueTestApp(_snapshot(), backend=backend)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await app.view.apply_filters(QueueFilters(stages=frozenset({JobStage.VALIDATE})))
            assert "Movie C.mkv" in app.view.table.content_text
            assert "Movie A.mkv" not in app.view.table.content_text

    asyncio.run(run())


def test_queue_filters_profile() -> None:
    async def run() -> None:
        backend = FakeQueueBackend(_snapshot())
        app = QueueTestApp(_snapshot(), backend=backend)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await app.view.apply_filters(QueueFilters(profile_name="animation"))
            assert "Movie C.mkv" in app.view.table.content_text
            assert "Movie B.mkv" not in app.view.table.content_text

    asyncio.run(run())


def test_queue_searches_file_name() -> None:
    async def run() -> None:
        backend = FakeQueueBackend(_snapshot())
        app = QueueTestApp(_snapshot(), backend=backend)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await app.view.apply_filters(QueueFilters(search_text="Movie B"))
            assert "Movie B.mkv" in app.view.table.content_text
            assert "Movie A.mkv" not in app.view.table.content_text

    asyncio.run(run())


def test_queue_opens_selected_job() -> None:
    async def run() -> None:
        app = QueueTestApp(_snapshot())
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.view.table.toggle_job(43)
            app.view.open_selected_job()
            await pilot.pause()
            assert app.opened_job_ids == [43]

    asyncio.run(run())


def test_queue_shows_phase_when_percentage_is_unknown() -> None:
    async def run() -> None:
        app = QueueTestApp(_snapshot())
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            assert "validating" in app.view.table.content_text

    asyncio.run(run())


def test_queue_does_not_invent_progress() -> None:
    async def run() -> None:
        app = QueueTestApp(_snapshot())
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            line = next(
                line for line in app.view.table.content_text.splitlines() if "Movie B.mkv" in line
            )
            assert "%" not in line
            assert "waiting" in line

    asyncio.run(run())


class QueueTestApp(App[None]):
    def __init__(
        self,
        snapshot: QueueSnapshot,
        *,
        backend: FakeQueueBackend | None = None,
    ) -> None:
        super().__init__()
        self.backend = backend or FakeQueueBackend(snapshot)
        self.view = QueueView(backend=self.backend, snapshot=snapshot)
        self.opened_job_ids: list[int] = []

    def compose(self) -> ComposeResult:
        yield self.view

    def on_queue_view_job_open_requested(self, event: QueueView.JobOpenRequested) -> None:
        self.opened_job_ids.append(event.job_id)


class FakeQueueBackend:
    def __init__(self, snapshot: QueueSnapshot) -> None:
        self.snapshot = snapshot
        self.calls: list[QueueFilters] = []

    async def get_queue_snapshot(self, filters: QueueFilters) -> QueueSnapshot:
        self.calls.append(filters)
        return QueueSnapshot(
            revision=self.snapshot.revision,
            scheduler=self.snapshot.scheduler,
            totals=self.snapshot.totals,
            filters=filters,
            rows=tuple(row for row in self.snapshot.rows if _matches(row, filters)),
        )


def _matches(row: QueueJobRow, filters: QueueFilters) -> bool:
    if filters.statuses is not None and JobStatus(row.status.lower()) not in filters.statuses:
        return False
    if filters.stages is not None and JobStage(row.stage) not in filters.stages:
        return False
    if filters.profile_name is not None and row.profile_name != filters.profile_name:
        return False
    return not (
        filters.search_text and filters.search_text.lower() not in row.file_name.lower()
    )


def _snapshot() -> QueueSnapshot:
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
        totals=QueueTotals(pending=1, running=1, failed=1, validated=1),
        filters=QueueFilters(),
        rows=(
            _row(42, "RUNNING", "encode", 100, 3, "54%", "my_1080p", "Movie A.mkv"),
            _row(43, "HELD", "validate", 0, 3, "waiting", "av1_1080p", "Movie B.mkv"),
            _row(44, "FAILED", "validate", 0, 4, "validating", "animation", "Movie C.mkv"),
            _row(45, "VALIDATED", "promote", 0, 4, "ready", "my_film", "Movie D.mkv"),
        ),
    )


def _row(
    job_id: int,
    status: str,
    stage: str,
    priority: int,
    attempts: int,
    progress: str,
    profile_name: str,
    file_name: str,
) -> QueueJobRow:
    return QueueJobRow(
        job_id=job_id,
        status=status,
        stage=stage,
        priority=priority,
        attempts=attempts,
        progress=progress,
        profile_name=profile_name,
        file_name=file_name,
        source_path=f"/media/{file_name}",
        updated_at=datetime.now(UTC),
    )
