from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

from textual.app import ComposeResult
from textual.message import Message
from textual.widgets import Button, Input, Static

from avarch.tui.models.workflow import (
    DirectoryListing,
    ScanRootState,
    ScanRootValidation,
)


class DirectoryBrowserBackend(Protocol):
    async def browse_directory(self, path: Path, *, show_hidden: bool) -> DirectoryListing:
        ...


class FileBrowser(Static):
    class SelectionChanged(Message):
        def __init__(self, roots: tuple[Path, ...]) -> None:
            super().__init__()
            self.roots = roots

    class ContinueRequested(Message):
        pass

    DEFAULT_CSS = """
    FileBrowser {
        height: 1fr;
        padding: 1;
    }

    FileBrowser Button {
        margin-right: 1;
        margin-top: 1;
    }
    """

    def __init__(
        self,
        *,
        backend: DirectoryBrowserBackend,
        start_path: Path | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.backend = backend
        self.current_path = (start_path or Path.home()).expanduser()
        self.show_hidden = False
        self.selected_roots: list[Path] = []
        self.listing: DirectoryListing | None = None
        self.content_text = ""
        self.typed_path = ""

    def compose(self) -> ComposeResult:
        yield Static("", id="file-browser-content")
        yield Input(placeholder="Path", id="file-browser-path")
        yield Button("Parent", id="file-browser-parent")
        yield Button("Home", id="file-browser-home")
        yield Button("Hidden", id="file-browser-hidden")
        yield Button("Add path", id="file-browser-add")
        yield Button("Next", id="file-browser-next")

    async def on_mount(self) -> None:
        await self.load(self.current_path)

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "file-browser-parent":
            await self.navigate_parent()
        elif event.button.id == "file-browser-home":
            await self.go_home()
        elif event.button.id == "file-browser-hidden":
            await self.toggle_hidden()
        elif event.button.id == "file-browser-add":
            path_input = self.query_one("#file-browser-path", Input)
            await self.add_typed_path(path_input.value)
        elif event.button.id == "file-browser-next":
            self.post_message(self.ContinueRequested())

    async def load(self, path: Path) -> None:
        self.listing = await self.backend.browse_directory(path, show_hidden=self.show_hidden)
        self.current_path = self.listing.path
        self._render_browser()

    async def enter_directory(self, path: Path) -> None:
        await self.load(path)

    async def navigate_parent(self) -> None:
        if self.listing is None or self.listing.parent is None:
            return
        await self.load(self.listing.parent)

    async def go_home(self) -> None:
        await self.load(Path.home())

    async def toggle_hidden(self) -> None:
        self.show_hidden = not self.show_hidden
        await self.load(self.current_path)

    def toggle_selected(self, path: Path) -> None:
        normalized = _display_path(path)
        for index, selected in enumerate(self.selected_roots):
            if _same_path(selected, normalized):
                del self.selected_roots[index]
                self._selection_changed()
                self._render_browser()
                return
        self.selected_roots.append(normalized)
        self._selection_changed()
        self._render_browser()

    async def add_typed_path(self, value: str) -> None:
        self.typed_path = value
        stripped = value.strip()
        if not stripped:
            self._render_browser()
            return
        path = Path(stripped).expanduser()
        if not path.is_absolute():
            path = self.current_path / path
        self.selected_roots.append(_display_path(path))
        self._selection_changed()
        self._render_browser()

    def validated_roots(self) -> tuple[ScanRootValidation, ...]:
        return validate_scan_roots(tuple(self.selected_roots))

    def normalized_selected_roots(self) -> tuple[Path, ...]:
        roots: list[Path] = []
        for validation in self.validated_roots():
            if validation.state == ScanRootState.VALID:
                roots.append(_safe_resolve(validation.path))
        return tuple(roots)

    def _selection_changed(self) -> None:
        self.post_message(self.SelectionChanged(tuple(self.selected_roots)))

    def _render_browser(self) -> None:
        text = self._render_text()
        self.content_text = text
        if self.is_mounted:
            self.query_one("#file-browser-content", Static).update(text)

    def _render_text(self) -> str:
        lines = [f"Path: {self.current_path}", ""]
        listing = self.listing
        if listing is None:
            lines.append("Loading...")
        else:
            if listing.parent is not None:
                lines.append("[ ] ../")
            directories = [entry for entry in listing.entries if entry.is_dir]
            if directories:
                for entry in directories:
                    marker = "x" if _contains_path(self.selected_roots, entry.path) else " "
                    hidden = " hidden" if entry.is_hidden else ""
                    lines.append(f"[{marker}] {entry.name}/{hidden}")
            else:
                lines.append("No folders in this directory.")

        lines.extend(["", "Selected roots:"])
        validations = self.validated_roots()
        if not validations:
            lines.append("  None selected.")
        for validation in validations:
            lines.append(f"  {validation.path} - {validation.state.value}: {validation.reason}")

        lines.extend(
            [
                "",
                f"Hidden entries: {'shown' if self.show_hidden else 'hidden'}",
                f"Typed path: {self.typed_path or '-'}",
            ]
        )
        return "\n".join(lines)


def validate_scan_roots(roots: tuple[Path, ...]) -> tuple[ScanRootValidation, ...]:
    validations: list[ScanRootValidation] = []
    resolved_so_far: list[Path] = []
    all_resolved = [_safe_resolve(root) for root in roots]

    for index, root in enumerate(roots):
        resolved = all_resolved[index]
        if not root.exists():
            validations.append(
                ScanRootValidation(root, ScanRootState.MISSING, "The path does not exist.")
            )
            continue
        if not root.is_dir():
            validations.append(
                ScanRootValidation(root, ScanRootState.UNSUPPORTED, "The path is not a folder.")
            )
            continue
        if not os.access(root, os.R_OK | os.X_OK):
            validations.append(
                ScanRootValidation(root, ScanRootState.UNREADABLE, "The folder is not readable.")
            )
            continue
        if any(_same_path(existing, resolved) for existing in resolved_so_far):
            validations.append(
                ScanRootValidation(
                    root,
                    ScanRootState.DUPLICATE,
                    "This folder is already selected.",
                )
            )
            continue
        parent = _selected_parent(resolved, all_resolved[:index])
        if parent is not None:
            validations.append(
                ScanRootValidation(
                    root,
                    ScanRootState.NESTED_DUPLICATE,
                    f"{parent} already covers this folder.",
                )
            )
            resolved_so_far.append(resolved)
            continue
        validations.append(
            ScanRootValidation(root, ScanRootState.VALID, "This folder can be scanned.")
        )
        resolved_so_far.append(resolved)

    return tuple(validations)


def _selected_parent(path: Path, earlier_roots: list[Path]) -> Path | None:
    for earlier in earlier_roots:
        try:
            path.relative_to(earlier)
        except ValueError:
            continue
        if path != earlier:
            return earlier
    return None


def _contains_path(paths: list[Path], path: Path) -> bool:
    normalized = _display_path(path)
    return any(_same_path(existing, normalized) for existing in paths)


def _same_path(left: Path, right: Path) -> bool:
    return _safe_resolve(left) == _safe_resolve(right)


def _display_path(path: Path) -> Path:
    return path.expanduser().absolute()


def _safe_resolve(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except OSError:
        return path.expanduser().absolute()
