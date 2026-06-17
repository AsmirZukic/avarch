from __future__ import annotations

from textual.app import ComposeResult
from textual.message import Message
from textual.widgets import Static

from avarch.tui.models.queue import QueueJobRow, QueueSnapshot


class QueueTable(Static):
    class SelectionChanged(Message):
        def __init__(self, job_ids: tuple[int, ...]) -> None:
            super().__init__()
            self.job_ids = job_ids

    class JobOpened(Message):
        def __init__(self, job_id: int) -> None:
            super().__init__()
            self.job_id = job_id

    def __init__(
        self,
        snapshot: QueueSnapshot,
        *,
        selected_job_ids: set[int] | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.snapshot = snapshot
        self.selected_job_ids = selected_job_ids or set()
        self.content_text = ""

    def compose(self) -> ComposeResult:
        yield Static("", id="queue-table-content")

    async def on_mount(self) -> None:
        self.render_table()

    def update_snapshot(self, snapshot: QueueSnapshot) -> None:
        self.snapshot = snapshot
        available_ids = {row.job_id for row in snapshot.rows}
        self.selected_job_ids.intersection_update(available_ids)
        self.render_table()

    def toggle_job(self, job_id: int) -> None:
        if self._row_by_id(job_id) is None:
            return
        if job_id in self.selected_job_ids:
            self.selected_job_ids.remove(job_id)
        else:
            self.selected_job_ids.add(job_id)
        self.post_message(self.SelectionChanged(tuple(sorted(self.selected_job_ids))))
        self.render_table()

    def open_job(self, job_id: int) -> None:
        if self._row_by_id(job_id) is not None:
            self.post_message(self.JobOpened(job_id))

    def first_selected_job_id(self) -> int | None:
        return min(self.selected_job_ids) if self.selected_job_ids else None

    def render_table(self) -> None:
        text = queue_table_text(self.snapshot.rows, self.selected_job_ids)
        self.content_text = text
        if self.is_mounted:
            self.query_one("#queue-table-content", Static).update(text)

    def _row_by_id(self, job_id: int) -> QueueJobRow | None:
        for row in self.snapshot.rows:
            if row.job_id == job_id:
                return row
        return None


def queue_table_text(rows: tuple[QueueJobRow, ...], selected_job_ids: set[int]) -> str:
    lines = ["SEL ID   STATUS     STAGE       PRI TRY PROGRESS     PROFILE       FILE"]
    if not rows:
        lines.append("No queued workflows match the current filters.")
        return "\n".join(lines)
    for row in rows:
        selected = "x" if row.job_id in selected_job_ids else " "
        lines.append(
            f"[{selected}] "
            f"{row.job_id:<4} "
            f"{row.status:<10} "
            f"{row.stage:<11} "
            f"{row.priority:<3} "
            f"{row.attempts:<3} "
            f"{row.progress:<12} "
            f"{row.profile_name:<13} "
            f"{row.file_name}"
        )
    return "\n".join(lines)
