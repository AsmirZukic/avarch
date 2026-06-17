from __future__ import annotations

import asyncio
from pathlib import Path

from textual.app import App, ComposeResult

from avarch.tui.models.workflow import (
    AnalysisFailure,
    AnalysisSummary,
    CandidateFilters,
    CandidateRow,
    CandidateSnapshot,
    CandidateState,
    DirectoryListing,
    ScanSummary,
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


def test_analyze_runs_for_selected_unprobed_media() -> None:
    async def run() -> None:
        draft = WorkflowDraft()
        backend = FakeAnalysisBackend(_snapshot(), _snapshot())
        app = CandidateReviewTestApp(_snapshot(), draft=draft, backend=backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.view.table.toggle_candidate(2)
            await pilot.pause()
            await app.view.analyze_selected()
            assert backend.analyze_calls == [(2,)]
            assert "Analysis result" in app.view.content_text

    asyncio.run(run())


def test_analyze_shows_per_file_progress() -> None:
    async def run() -> None:
        backend = FakeAnalysisBackend(_snapshot(), _snapshot())
        backend.block_analysis = True
        app = CandidateReviewTestApp(_snapshot(), draft=WorkflowDraft(), backend=backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.view.table.toggle_candidate(1)
            app.view.table.toggle_candidate(2)
            await pilot.pause()
            task = asyncio.create_task(app.view.analyze_selected())
            await backend.analysis_started.wait()
            assert "Analyzing 2 files" in app.view.content_text
            backend.release_analysis.set()
            await task

    asyncio.run(run())


def test_successful_analysis_refreshes_candidates() -> None:
    refreshed = CandidateSnapshot(
        rows=(
            CandidateRow(
                media_file_id=2,
                path="/media/movies/Movie B.mkv",
                state=CandidateState.READY,
                reason="Video is HEVC",
                eligible=True,
            ),
        )
    )

    async def run() -> None:
        backend = FakeAnalysisBackend(_snapshot(), refreshed)
        app = CandidateReviewTestApp(_snapshot(), draft=WorkflowDraft(), backend=backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.view.table.toggle_candidate(2)
            await pilot.pause()
            await app.view.analyze_selected()
            assert "Video is HEVC" in app.view.table.content_text
            assert "NEEDS ANALYSIS" not in app.view.table.content_text

    asyncio.run(run())


def test_partial_failure_keeps_successful_results() -> None:
    summary = AnalysisSummary(
        requested=2,
        completed=1,
        failed=1,
        failures=(
            AnalysisFailure(
                media_file_id=2,
                path="/media/movies/Movie B.mkv",
                error="ffprobe failed",
            ),
        ),
    )

    async def run() -> None:
        backend = FakeAnalysisBackend(_snapshot(), _snapshot(), summary=summary)
        app = CandidateReviewTestApp(_snapshot(), draft=WorkflowDraft(), backend=backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.view.table.toggle_candidate(1)
            app.view.table.toggle_candidate(2)
            await pilot.pause()
            await app.view.analyze_selected()
            assert "1 / 2 complete" in app.view.content_text
            assert "ffprobe failed" in app.view.content_text
            assert backend.refresh_calls == [()]

    asyncio.run(run())


class CandidateTableTestApp(App[None]):
    def __init__(self, snapshot: CandidateSnapshot) -> None:
        super().__init__()
        self.table = CandidateTable(snapshot)

    def compose(self) -> ComposeResult:
        yield self.table


class CandidateReviewTestApp(App[None]):
    def __init__(
        self,
        snapshot: CandidateSnapshot,
        *,
        draft: WorkflowDraft,
        backend: FakeAnalysisBackend | None = None,
    ) -> None:
        super().__init__()
        self.view = WorkflowCandidateReviewView(
            snapshot=snapshot,
            draft=draft,
            backend=backend,
        )

    def compose(self) -> ComposeResult:
        yield self.view


class FakeAnalysisBackend:
    def __init__(
        self,
        initial: CandidateSnapshot,
        refreshed: CandidateSnapshot,
        *,
        summary: AnalysisSummary | None = None,
    ) -> None:
        self.initial = initial
        self.refreshed = refreshed
        self.summary = summary
        self.analyze_calls: list[tuple[int, ...]] = []
        self.refresh_calls: list[tuple[Path, ...]] = []
        self.block_analysis = False
        self.analysis_started = asyncio.Event()
        self.release_analysis = asyncio.Event()

    async def browse_directory(
        self,
        path: Path,
        *,
        show_hidden: bool,
    ) -> DirectoryListing:
        del path, show_hidden
        raise NotImplementedError

    async def scan_roots(self, roots: tuple[Path, ...]) -> ScanSummary:
        del roots
        raise NotImplementedError

    async def list_workflow_candidates(self, roots: tuple[Path, ...]) -> CandidateSnapshot:
        self.refresh_calls.append(roots)
        return self.refreshed

    async def analyze_media(self, media_file_ids: tuple[int, ...]) -> AnalysisSummary:
        self.analyze_calls.append(media_file_ids)
        self.analysis_started.set()
        if self.block_analysis:
            await self.release_analysis.wait()
        return self.summary or AnalysisSummary(
            requested=len(media_file_ids),
            completed=len(media_file_ids),
            failed=0,
        )


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
