from __future__ import annotations

from pathlib import Path
from typing import Protocol

from textual.app import ComposeResult
from textual.message import Message
from textual.widgets import Button, Static

from avarch.tui.models.workflow import DirectoryListing, ScanSummary, WorkflowDraft
from avarch.tui.widgets.file_browser import FileBrowser


class WorkflowScanBackend(Protocol):
    async def browse_directory(self, path: Path, *, show_hidden: bool) -> DirectoryListing:
        ...

    async def scan_roots(self, roots: tuple[Path, ...]) -> ScanSummary:
        ...


class WorkflowScanView(Static):
    class Canceled(Message):
        pass

    class ScanCompleted(Message):
        def __init__(self, summary: ScanSummary) -> None:
            super().__init__()
            self.summary = summary

    DEFAULT_CSS = """
    WorkflowScanView {
        height: 1fr;
        padding: 1;
    }

    WorkflowScanView Button {
        margin-right: 1;
        margin-top: 1;
    }
    """

    def __init__(
        self,
        *,
        backend: WorkflowScanBackend,
        draft: WorkflowDraft | None = None,
        start_path: Path | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.backend = backend
        self.draft = draft or WorkflowDraft()
        self.browser = FileBrowser(backend=backend, start_path=start_path)
        self.phase = "Select folders"
        self.error_message: str | None = None
        self.content_text = ""

    def compose(self) -> ComposeResult:
        yield Static("", id="workflow-scan-status")
        yield self.browser
        yield Button("Scan selected folders", id="workflow-scan")
        yield Button("Cancel", id="workflow-cancel")

    async def on_mount(self) -> None:
        self._render_status()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "workflow-scan":
            await self.run_scan()
            event.stop()
        elif event.button.id == "workflow-cancel":
            self.cancel()
            event.stop()

    def on_file_browser_selection_changed(self, event: FileBrowser.SelectionChanged) -> None:
        self.draft.roots = self.browser.normalized_selected_roots()
        self._render_status()
        event.stop()

    async def run_scan(self) -> None:
        roots = self.browser.normalized_selected_roots()
        self.draft.roots = roots
        if not roots:
            self.error_message = "Select at least one valid folder before scanning."
            self.phase = "Selection required"
            self._render_status()
            return

        self.error_message = None
        self.phase = f"Scanning {len(roots)} folder(s)"
        self._render_status()
        try:
            summary = await self.backend.scan_roots(roots)
        except Exception as exc:
            self.error_message = str(exc) or exc.__class__.__name__
            self.phase = "Scan failed"
            self._render_status()
            return

        self.draft.scan_summary = summary
        self.phase = "Scan complete"
        self._render_status()
        self.post_message(self.ScanCompleted(summary))

    def cancel(self) -> None:
        self.draft = WorkflowDraft()
        self.error_message = None
        self.phase = "Canceled"
        self._render_status()
        self.post_message(self.Canceled())

    def _render_status(self) -> None:
        text = self._status_text()
        self.content_text = text
        if self.is_mounted:
            self.query_one("#workflow-scan-status", Static).update(text)

    def _status_text(self) -> str:
        lines = [
            "New Workflow - Scan",
            "",
            "Scanning updates Avarch's media inventory.",
            "It reads directory entries and file metadata.",
            "It does not encode, rename, replace, or delete media.",
            "",
            f"Phase: {self.phase}",
            f"Selected folders: {len(self.draft.roots)}",
        ]
        if self.error_message is not None:
            lines.extend(["", f"Error: {self.error_message}"])
        if self.draft.scan_summary is not None:
            summary = self.draft.scan_summary
            lines.extend(
                [
                    "",
                    "Scan result",
                    f"  Roots scanned: {len(summary.roots)}",
                    f"  New: {summary.added}",
                    f"  Changed: {summary.changed}",
                    f"  Missing: {summary.missing}",
                    f"  Unchanged: {summary.unchanged}",
                ]
            )
        return "\n".join(lines)
