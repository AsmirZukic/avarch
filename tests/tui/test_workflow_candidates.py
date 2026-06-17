from __future__ import annotations

import asyncio

from textual.app import App, ComposeResult

from avarch.tui.models.workflow import (
    CandidateFilters,
    CandidateRow,
    CandidateSnapshot,
    CandidateState,
    WorkflowDraft,
)
from avarch.tui.screens.workflow import WorkflowCandidateReviewView
from avarch.tui.widgets.candidate_table import CandidateTable


def test_candidate_table_shows_decision_reason() -> None:
    async def run() -> None:
        app = CandidateTableTestApp(_snapshot())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            text = app.table.content_text
            assert "Movie A.mkv" in text
            assert "READY" in text
            assert "Video is H.264" in text

    asyncio.run(run())


def test_ready_candidate_can_be_selected() -> None:
    async def run() -> None:
        app = CandidateTableTestApp(_snapshot())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.table.toggle_candidate(1)
            assert app.table.selected_media_ids == {1}
            assert "[x]" in app.table.content_text

    asyncio.run(run())


def test_blocked_candidate_cannot_be_selected() -> None:
    async def run() -> None:
        app = CandidateTableTestApp(_snapshot())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.table.toggle_candidate(4)
            assert app.table.selected_media_ids == set()
            assert "Movie D.mkv" in app.table.content_text
            assert "BLOCKED" in app.table.content_text

    asyncio.run(run())


def test_select_all_selects_only_eligible_rows() -> None:
    async def run() -> None:
        app = CandidateTableTestApp(_snapshot())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.table.select_all_eligible()
            assert app.table.selected_media_ids == {1, 2}

    asyncio.run(run())


def test_candidate_filters_work() -> None:
    async def run() -> None:
        app = CandidateTableTestApp(_snapshot())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.table.set_filters(
                CandidateFilters(states=frozenset({CandidateState.NEEDS_ANALYSIS}))
            )
            text = app.table.content_text
            assert "Movie B.mkv" in text
            assert "Movie A.mkv" not in text

    asyncio.run(run())


def test_candidate_search_filters_paths() -> None:
    async def run() -> None:
        app = CandidateTableTestApp(_snapshot())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.table.set_filters(CandidateFilters(search_text="episode"))
            text = app.table.content_text
            assert "Episode 01.mkv" in text
            assert "Movie A.mkv" not in text

    asyncio.run(run())


def test_media_detail_shows_probe_summary() -> None:
    table = CandidateTable(_snapshot())

    detail = table.detail_text(1)

    assert "Media detail" in detail
    assert "Path: /media/movies/Movie A.mkv" in detail
    assert "Decision: READY" in detail
    assert "Reason: Video is H.264" in detail


def test_candidate_review_updates_workflow_draft_selection() -> None:
    async def run() -> None:
        draft = WorkflowDraft()
        app = CandidateReviewTestApp(_snapshot(), draft=draft)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.view.table.toggle_candidate(1)
            await pilot.pause()
            assert draft.selected_media_ids == {1}
            assert "Selected: 1" in app.view.content_text

    asyncio.run(run())


class CandidateTableTestApp(App[None]):
    def __init__(self, snapshot: CandidateSnapshot) -> None:
        super().__init__()
        self.table = CandidateTable(snapshot)

    def compose(self) -> ComposeResult:
        yield self.table


class CandidateReviewTestApp(App[None]):
    def __init__(self, snapshot: CandidateSnapshot, *, draft: WorkflowDraft) -> None:
        super().__init__()
        self.view = WorkflowCandidateReviewView(snapshot=snapshot, draft=draft)

    def compose(self) -> ComposeResult:
        yield self.view


def _snapshot() -> CandidateSnapshot:
    return CandidateSnapshot(
        rows=(
            CandidateRow(
                media_file_id=1,
                path="/media/movies/Movie A.mkv",
                state=CandidateState.READY,
                reason="Video is H.264",
                eligible=True,
            ),
            CandidateRow(
                media_file_id=2,
                path="/media/movies/Movie B.mkv",
                state=CandidateState.NEEDS_ANALYSIS,
                reason="No current probe",
                eligible=True,
            ),
            CandidateRow(
                media_file_id=3,
                path="/media/movies/Movie C.mkv",
                state=CandidateState.ALREADY_SATISFIED,
                reason="Already AV1",
                eligible=False,
            ),
            CandidateRow(
                media_file_id=4,
                path="/media/movies/Movie D.mkv",
                state=CandidateState.BLOCKED,
                reason="HDR rejected",
                eligible=False,
            ),
            CandidateRow(
                media_file_id=5,
                path="/media/series/Episode 01.mkv",
                state=CandidateState.EXCLUDED,
                reason="Scanner exclusion rule",
                eligible=False,
            ),
        )
    )
