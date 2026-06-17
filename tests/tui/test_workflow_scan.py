from __future__ import annotations

import asyncio
from pathlib import Path

from textual.app import App, ComposeResult

from avarch.tui.models.workflow import DirectoryEntry, DirectoryListing, ScanSummary
from avarch.tui.screens.workflow import WorkflowScanView


def test_scan_requires_selected_root(tmp_path: Path) -> None:
    async def run() -> None:
        app = WorkflowScanTestApp(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.view.run_scan()
            assert app.backend.scan_calls == []
            assert "Select at least one valid folder" in app.view.content_text

    asyncio.run(run())


def test_scan_explains_read_only_media_behavior(tmp_path: Path) -> None:
    async def run() -> None:
        app = WorkflowScanTestApp(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            text = app.view.content_text
            assert "updates Avarch's media inventory" in text
            assert "does not encode, rename, replace, or delete media" in text

    asyncio.run(run())


def test_scan_calls_backend_with_all_roots(tmp_path: Path) -> None:
    first = tmp_path / "Movies"
    second = tmp_path / "Series"
    first.mkdir()
    second.mkdir()

    async def run() -> None:
        app = WorkflowScanTestApp(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.view.browser.toggle_selected(first)
            app.view.browser.toggle_selected(second)
            await app.view.run_scan()
            assert app.backend.scan_calls == [(first.resolve(), second.resolve())]

    asyncio.run(run())


def test_scan_shows_progress(tmp_path: Path) -> None:
    root = tmp_path / "Movies"
    root.mkdir()

    async def run() -> None:
        app = WorkflowScanTestApp(tmp_path)
        app.backend.block_scan = True
        async with app.run_test(size=(100, 30)) as _pilot:
            app.view.browser.toggle_selected(root)
            scan_task = asyncio.create_task(app.view.run_scan())
            await app.backend.scan_started.wait()
            assert "Phase: Scanning 1 folder(s)" in app.view.content_text
            app.backend.release_scan.set()
            await scan_task

    asyncio.run(run())


def test_scan_shows_new_changed_missing_counts(tmp_path: Path) -> None:
    root = tmp_path / "Movies"
    root.mkdir()

    async def run() -> None:
        app = WorkflowScanTestApp(
            tmp_path,
            summary=ScanSummary(
                roots=(root.resolve(),),
                added=12,
                changed=3,
                missing=1,
                unchanged=148,
            ),
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.view.browser.toggle_selected(root)
            await app.view.run_scan()
            text = app.view.content_text
            assert "Scan complete" in text
            assert "New: 12" in text
            assert "Changed: 3" in text
            assert "Missing: 1" in text
            assert "Unchanged: 148" in text

    asyncio.run(run())


def test_scan_failure_preserves_folder_selection(tmp_path: Path) -> None:
    root = tmp_path / "Movies"
    root.mkdir()

    async def run() -> None:
        app = WorkflowScanTestApp(tmp_path, failure=RuntimeError("disk unavailable"))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.view.browser.toggle_selected(root)
            await app.view.run_scan()
            assert app.view.draft.roots == (root.resolve(),)
            assert app.view.browser.normalized_selected_roots() == (root.resolve(),)
            assert "disk unavailable" in app.view.content_text

    asyncio.run(run())


def test_cancel_after_scan_discards_draft_only(tmp_path: Path) -> None:
    root = tmp_path / "Movies"
    root.mkdir()

    async def run() -> None:
        app = WorkflowScanTestApp(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.view.browser.toggle_selected(root)
            await app.view.run_scan()
            assert app.view.draft.scan_summary is not None
            app.view.cancel()
            assert app.view.draft.roots == ()
            assert app.view.draft.scan_summary is None
            assert app.backend.scan_calls == [(root.resolve(),)]

    asyncio.run(run())


class WorkflowScanTestApp(App[None]):
    def __init__(
        self,
        start_path: Path,
        *,
        summary: ScanSummary | None = None,
        failure: Exception | None = None,
    ) -> None:
        super().__init__()
        self.backend = FakeWorkflowScanBackend(summary=summary, failure=failure)
        self.view = WorkflowScanView(backend=self.backend, start_path=start_path)

    def compose(self) -> ComposeResult:
        yield self.view


class FakeWorkflowScanBackend:
    def __init__(
        self,
        *,
        summary: ScanSummary | None = None,
        failure: Exception | None = None,
    ) -> None:
        self.summary = summary
        self.failure = failure
        self.scan_calls: list[tuple[Path, ...]] = []
        self.block_scan = False
        self.scan_started = asyncio.Event()
        self.release_scan = asyncio.Event()

    async def browse_directory(self, path: Path, *, show_hidden: bool) -> DirectoryListing:
        resolved = path.expanduser().resolve()
        entries: list[DirectoryEntry] = []
        for entry in sorted(resolved.iterdir(), key=lambda item: item.name):
            is_hidden = entry.name.startswith(".")
            if is_hidden and not show_hidden:
                continue
            entries.append(
                DirectoryEntry(
                    path=entry.resolve(),
                    name=entry.name,
                    is_dir=entry.is_dir(),
                    is_hidden=is_hidden,
                )
            )
        return DirectoryListing(
            path=resolved,
            parent=resolved.parent if resolved.parent != resolved else None,
            entries=tuple(entries),
        )

    async def scan_roots(self, roots: tuple[Path, ...]) -> ScanSummary:
        self.scan_calls.append(roots)
        self.scan_started.set()
        if self.block_scan:
            await self.release_scan.wait()
        if self.failure is not None:
            raise self.failure
        return self.summary or ScanSummary(
            roots=roots,
            added=1,
            changed=0,
            missing=0,
            unchanged=0,
        )
