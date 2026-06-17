from __future__ import annotations

from pathlib import Path
from typing import Protocol

from textual.app import ComposeResult
from textual.message import Message
from textual.widgets import Button, Static

from avarch.tui.models.library import (
    LibraryFilters,
    LibraryRow,
    LibrarySnapshot,
)
from avarch.tui.models.workflow import AnalysisSummary, WorkflowDraft


class LibraryBackend(Protocol):
    async def list_library(self) -> LibrarySnapshot:
        ...

    async def analyze_media(self, media_file_ids: tuple[int, ...]) -> AnalysisSummary:
        ...


class LibraryView(Static):
    class WorkflowRequested(Message):
        def __init__(self, draft: WorkflowDraft) -> None:
            super().__init__()
            self.draft = draft

    DEFAULT_CSS = """
    LibraryView {
        height: 1fr;
        padding: 1;
    }

    LibraryView Button {
        margin-right: 1;
        margin-top: 1;
    }
    """

    def __init__(
        self,
        *,
        backend: LibraryBackend,
        snapshot: LibrarySnapshot | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.backend = backend
        self.snapshot = snapshot or LibrarySnapshot(rows=())
        self.filters = LibraryFilters()
        self.selected_media_ids: set[int] = set()
        self.phase = "Library"
        self.error_message: str | None = None
        self.analysis_summary: AnalysisSummary | None = None
        self.content_text = ""

    def compose(self) -> ComposeResult:
        yield Static("", id="library-status")
        yield Static("", id="library-table")
        yield Button("Analyze selected", id="library-analyze")
        yield Button("Start workflow", id="library-start-workflow")

    async def on_mount(self) -> None:
        if not self.snapshot.rows:
            await self.refresh_library()
        else:
            self._render_library()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "library-analyze":
            self.run_worker(self.analyze_selected(), exclusive=True)
            event.stop()
        elif event.button.id == "library-start-workflow":
            self.start_workflow_with_selection()
            event.stop()

    async def refresh_library(self) -> None:
        self.phase = "Loading library"
        self.error_message = None
        self._render_library()
        try:
            self.snapshot = await self.backend.list_library()
        except Exception as exc:
            self.phase = "Library load failed"
            self.error_message = str(exc) or exc.__class__.__name__
            self._render_library()
            return
        self.phase = "Library"
        self._sync_available_selection()
        self._render_library()

    def set_filters(self, filters: LibraryFilters) -> None:
        self.filters = filters
        self._sync_available_selection()
        self._render_library()

    def toggle_media(self, media_file_id: int) -> None:
        row = self._row_by_id(media_file_id)
        if row is None or not row.selectable:
            return
        if media_file_id in self.selected_media_ids:
            self.selected_media_ids.remove(media_file_id)
        else:
            self.selected_media_ids.add(media_file_id)
        self._render_library()

    async def analyze_selected(self) -> None:
        selected = tuple(sorted(self.selected_media_ids))
        if not selected:
            self.phase = "Selection required"
            self.error_message = "Select at least one media file before analysis."
            self._render_library()
            return
        self.phase = f"Analyzing {len(selected)} files"
        self.error_message = None
        self.analysis_summary = await self.backend.analyze_media(selected)
        self.snapshot = await self.backend.list_library()
        self._sync_available_selection()
        self.phase = "Analysis complete"
        self._render_library()

    def start_workflow_with_selection(self) -> None:
        selected = tuple(sorted(self.selected_media_ids))
        if not selected:
            self.phase = "Selection required"
            self.error_message = "Select at least one media file before starting a workflow."
            self._render_library()
            return
        draft = WorkflowDraft(selected_media_ids=set(selected))
        self.post_message(self.WorkflowRequested(draft))

    def filtered_rows(self) -> tuple[LibraryRow, ...]:
        rows: list[LibraryRow] = []
        search_text = self.filters.search_text.lower() if self.filters.search_text else None
        codec = self.filters.video_codec.lower() if self.filters.video_codec else None
        for row in self.snapshot.rows:
            if (
                self.filters.inventory_statuses is not None
                and row.inventory_status not in self.filters.inventory_statuses
            ):
                continue
            if (
                self.filters.probe_states is not None
                and row.probe_state not in self.filters.probe_states
            ):
                continue
            if codec is not None and (row.video_codec or "").lower() != codec:
                continue
            if self.filters.root is not None and not _path_in_root(row.path, self.filters.root):
                continue
            if search_text is not None and search_text not in row.path.lower():
                continue
            rows.append(row)
        return tuple(rows)

    def _render_library(self) -> None:
        status = self._status_text()
        table = self._table_text()
        self.content_text = "\n\n".join([status, table])
        if self.is_mounted:
            self.query_one("#library-status", Static).update(status)
            self.query_one("#library-table", Static).update(table)

    def _status_text(self) -> str:
        lines = [
            "Library",
            "",
            f"Phase: {self.phase}",
            f"Inventory files: {len(self.snapshot.rows)}",
            f"Visible: {len(self.filtered_rows())}",
            f"Selected: {len(self.selected_media_ids)}",
            "Direct enqueue: disabled",
        ]
        if self.error_message is not None:
            lines.extend(["", f"Error: {self.error_message}"])
        if self.analysis_summary is not None:
            summary = self.analysis_summary
            lines.extend(
                [
                    "",
                    "Analysis result",
                    f"  Requested: {summary.requested}",
                    f"  Completed: {summary.completed}",
                    f"  Failed: {summary.failed}",
                ]
            )
        return "\n".join(lines)

    def _table_text(self) -> str:
        lines = [
            (
                "SEL  STATUS    PROBE      CODEC      RESOLUTION   "
                "DURATION   LAST SCANNED          FILE"
            )
        ]
        rows = self.filtered_rows()
        if not rows:
            lines.append("No media files match the current filters.")
            return "\n".join(lines)
        for row in rows:
            selected = "x" if row.media_file_id in self.selected_media_ids else " "
            duration = _duration_text(row.duration_seconds)
            lines.append(
                f"[{selected}]  "
                f"{row.inventory_status:<9} "
                f"{row.probe_state.value:<10} "
                f"{(row.video_codec or '-'): <10} "
                f"{(row.resolution or '-'): <12} "
                f"{duration:<10} "
                f"{row.last_scanned_at[:19]:<21} "
                f"{Path(row.path).name}"
            )
        return "\n".join(lines)

    def _row_by_id(self, media_file_id: int) -> LibraryRow | None:
        for row in self.snapshot.rows:
            if row.media_file_id == media_file_id:
                return row
        return None

    def _sync_available_selection(self) -> None:
        available = {row.media_file_id for row in self.filtered_rows() if row.selectable}
        self.selected_media_ids.intersection_update(available)


def _path_in_root(path: str, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _duration_text(duration_seconds: float | None) -> str:
    if duration_seconds is None:
        return "-"
    minutes, seconds = divmod(int(duration_seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"
