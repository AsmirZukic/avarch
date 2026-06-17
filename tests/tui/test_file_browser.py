from __future__ import annotations

import asyncio
from pathlib import Path

from textual.app import App, ComposeResult

from avarch.tui.models.workflow import DirectoryEntry, DirectoryListing, ScanRootState
from avarch.tui.widgets.file_browser import FileBrowser


def test_browser_lists_directories(tmp_path: Path) -> None:
    (tmp_path / "Movies").mkdir()
    (tmp_path / "clip.mkv").write_bytes(b"media")

    async def run() -> None:
        app = BrowserTestApp(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            browser = app.browser
            assert "Movies/" in browser.content_text
            assert "clip.mkv" not in browser.content_text

    asyncio.run(run())


def test_browser_enters_directory(tmp_path: Path) -> None:
    child = tmp_path / "Movies"
    child.mkdir()

    async def run() -> None:
        app = BrowserTestApp(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.browser.enter_directory(child)
            assert app.browser.current_path == child.resolve()
            assert f"Path: {child.resolve()}" in app.browser.content_text

    asyncio.run(run())


def test_browser_navigates_parent(tmp_path: Path) -> None:
    child = tmp_path / "Movies"
    child.mkdir()

    async def run() -> None:
        app = BrowserTestApp(child)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.browser.navigate_parent()
            assert app.browser.current_path == tmp_path.resolve()

    asyncio.run(run())


def test_browser_selects_multiple_folders(tmp_path: Path) -> None:
    movies = tmp_path / "Movies"
    series = tmp_path / "Series"
    movies.mkdir()
    series.mkdir()

    async def run() -> None:
        app = BrowserTestApp(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.browser.toggle_selected(movies)
            app.browser.toggle_selected(series)
            assert app.browser.normalized_selected_roots() == (
                movies.resolve(),
                series.resolve(),
            )
            assert "valid root" in app.browser.content_text

    asyncio.run(run())


def test_selection_survives_navigation(tmp_path: Path) -> None:
    movies = tmp_path / "Movies"
    other = tmp_path / "Other"
    nested = other / "Nested"
    movies.mkdir()
    nested.mkdir(parents=True)

    async def run() -> None:
        app = BrowserTestApp(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.browser.toggle_selected(movies)
            await app.browser.enter_directory(other)
            app.browser.toggle_selected(nested)
            assert movies.resolve() in app.browser.normalized_selected_roots()
            assert nested.resolve() in app.browser.normalized_selected_roots()

    asyncio.run(run())


def test_hidden_entries_are_optional(tmp_path: Path) -> None:
    hidden = tmp_path / ".secret"
    visible = tmp_path / "Visible"
    hidden.mkdir()
    visible.mkdir()

    async def run() -> None:
        app = BrowserTestApp(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert "Visible/" in app.browser.content_text
            assert ".secret/" not in app.browser.content_text
            await app.browser.toggle_hidden()
            assert ".secret/" in app.browser.content_text

    asyncio.run(run())


def test_typed_path_is_validated(tmp_path: Path) -> None:
    valid = tmp_path / "Movies"
    nested = valid / "Nested"
    file_path = tmp_path / "movie.mkv"
    valid.mkdir()
    nested.mkdir()
    file_path.write_bytes(b"media")

    async def run() -> None:
        app = BrowserTestApp(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.browser.add_typed_path(str(valid))
            await app.browser.add_typed_path(str(valid))
            await app.browser.add_typed_path(str(nested))
            await app.browser.add_typed_path(str(file_path))
            await app.browser.add_typed_path(str(tmp_path / "missing"))
            states = [validation.state for validation in app.browser.validated_roots()]
            assert states == [
                ScanRootState.VALID,
                ScanRootState.DUPLICATE,
                ScanRootState.NESTED_DUPLICATE,
                ScanRootState.UNSUPPORTED,
                ScanRootState.MISSING,
            ]

    asyncio.run(run())


def test_browser_performs_no_filesystem_mutation(tmp_path: Path) -> None:
    (tmp_path / "Movies").mkdir()
    before = _filesystem_snapshot(tmp_path)

    async def run() -> None:
        app = BrowserTestApp(tmp_path)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.browser.toggle_hidden()
            await app.browser.navigate_parent()
            await app.browser.add_typed_path(str(tmp_path / "Movies"))

    asyncio.run(run())

    assert _filesystem_snapshot(tmp_path) == before


class BrowserTestApp(App[None]):
    def __init__(self, start_path: Path) -> None:
        super().__init__()
        self.backend = FakeDirectoryBackend()
        self.browser = FileBrowser(backend=self.backend, start_path=start_path)

    def compose(self) -> ComposeResult:
        yield self.browser


class FakeDirectoryBackend:
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


def _filesystem_snapshot(root: Path) -> tuple[tuple[str, bool, int], ...]:
    return tuple(
        sorted(
            (
                str(path.relative_to(root)),
                path.is_dir(),
                path.stat().st_size,
            )
            for path in root.rglob("*")
        )
    )
