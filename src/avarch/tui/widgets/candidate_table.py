from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.message import Message
from textual.widgets import Static

from avarch.tui.models.workflow import (
    CandidateFilters,
    CandidateRow,
    CandidateSnapshot,
    CandidateState,
)


class CandidateTable(Static):
    class SelectionChanged(Message):
        def __init__(self, media_file_ids: tuple[int, ...]) -> None:
            super().__init__()
            self.media_file_ids = media_file_ids

    def __init__(
        self,
        snapshot: CandidateSnapshot,
        *,
        selected_media_ids: set[int] | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.snapshot = snapshot
        self.selected_media_ids = selected_media_ids or set()
        self.filters = CandidateFilters()
        self.content_text = ""

    def compose(self) -> ComposeResult:
        yield Static("", id="candidate-table-content")

    async def on_mount(self) -> None:
        self.render_table()

    def set_filters(self, filters: CandidateFilters) -> None:
        self.filters = filters
        self.render_table()

    def filtered_rows(self) -> tuple[CandidateRow, ...]:
        rows: list[CandidateRow] = []
        search_text = self.filters.search_text.lower() if self.filters.search_text else None
        for row in self.snapshot.rows:
            if self.filters.selected_only and row.media_file_id not in self.selected_media_ids:
                continue
            if self.filters.states is not None and row.state not in self.filters.states:
                continue
            if self.filters.folder is not None:
                try:
                    Path(row.path).resolve().relative_to(self.filters.folder.resolve())
                except ValueError:
                    continue
            if search_text is not None and search_text not in row.path.lower():
                continue
            rows.append(row)
        return tuple(rows)

    def toggle_candidate(self, media_file_id: int) -> None:
        row = self._row_by_id(media_file_id)
        if row is None or not row.eligible:
            return
        if media_file_id in self.selected_media_ids:
            self.selected_media_ids.remove(media_file_id)
        else:
            self.selected_media_ids.add(media_file_id)
        self._selection_changed()
        self.render_table()

    def select_all_eligible(self) -> None:
        for row in self.filtered_rows():
            if row.eligible:
                self.selected_media_ids.add(row.media_file_id)
        self._selection_changed()
        self.render_table()

    def clear_selection(self) -> None:
        self.selected_media_ids.clear()
        self._selection_changed()
        self.render_table()

    def detail_text(self, media_file_id: int) -> str:
        row = self._row_by_id(media_file_id)
        if row is None:
            return "Media file not found."
        return "\n".join(
            [
                "Media detail",
                f"Path: {row.path}",
                f"Decision: {_state_label(row.state)}",
                f"Reason: {row.reason}",
                f"Selectable: {'yes' if row.eligible else 'no'}",
            ]
        )

    def render_table(self) -> None:
        text = self._table_text()
        self.content_text = text
        if self.is_mounted:
            self.query_one("#candidate-table-content", Static).update(text)

    def _table_text(self) -> str:
        lines = ["SEL  STATE              FILE                  REASON"]
        rows = self.filtered_rows()
        if not rows:
            lines.append("No media candidates match the current filters.")
            return "\n".join(lines)
        for row in rows:
            selected = "x" if row.media_file_id in self.selected_media_ids else " "
            file_name = Path(row.path).name
            lines.append(
                f"[{selected}]  {_state_label(row.state):<17} {file_name:<21} {row.reason}"
            )
        return "\n".join(lines)

    def _row_by_id(self, media_file_id: int) -> CandidateRow | None:
        for row in self.snapshot.rows:
            if row.media_file_id == media_file_id:
                return row
        return None

    def _selection_changed(self) -> None:
        self.post_message(self.SelectionChanged(tuple(sorted(self.selected_media_ids))))


def _state_label(state: CandidateState) -> str:
    return {
        CandidateState.READY: "READY",
        CandidateState.NEEDS_ANALYSIS: "NEEDS ANALYSIS",
        CandidateState.ALREADY_SATISFIED: "ALREADY DONE",
        CandidateState.BLOCKED: "BLOCKED",
        CandidateState.EXCLUDED: "EXCLUDED",
    }[state]
