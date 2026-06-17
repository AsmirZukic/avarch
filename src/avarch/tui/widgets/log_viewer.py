from __future__ import annotations

from pathlib import Path
from typing import Protocol

from textual.app import ComposeResult
from textual.widgets import Static

from avarch.tui.models.jobs import JobLogSnapshot

DEFAULT_LOG_TAIL_BYTES = 64 * 1024


class LogBackend(Protocol):
    async def get_job_log_tail(
        self,
        *,
        job_id: int,
        attempt_number: int | None,
        tail_bytes: int,
    ) -> JobLogSnapshot:
        ...


class LogViewer(Static):
    def __init__(
        self,
        *,
        backend: LogBackend,
        job_id: int,
        attempt_numbers: tuple[int, ...],
        running_attempt_numbers: frozenset[int],
        tail_bytes: int = DEFAULT_LOG_TAIL_BYTES,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.backend = backend
        self.job_id = job_id
        self.attempt_numbers = tuple(sorted(attempt_numbers))
        self.running_attempt_numbers = running_attempt_numbers
        self.tail_bytes = tail_bytes
        self.selected_attempt_number = self.attempt_numbers[-1] if self.attempt_numbers else None
        self.snapshot: JobLogSnapshot | None = None
        self.content_text = self._content_text()

    def compose(self) -> ComposeResult:
        yield Static(self.content_text, id="log-viewer-content")

    async def on_mount(self) -> None:
        await self.load()

    def select_attempt(self, attempt_number: int | None) -> None:
        if attempt_number is not None and attempt_number not in self.attempt_numbers:
            return
        self.selected_attempt_number = attempt_number
        self.render_content()

    async def load(self) -> JobLogSnapshot:
        self.snapshot = await self.backend.get_job_log_tail(
            job_id=self.job_id,
            attempt_number=self.selected_attempt_number,
            tail_bytes=self.tail_bytes,
        )
        self.render_content()
        return self.snapshot

    def should_auto_refresh(self) -> bool:
        return (
            self.selected_attempt_number is not None
            and self.selected_attempt_number in self.running_attempt_numbers
        )

    async def maybe_auto_refresh(self) -> JobLogSnapshot | None:
        if not self.should_auto_refresh():
            return None
        return await self.load()

    def render_content(self) -> None:
        self.content_text = self._content_text()
        if self.is_mounted:
            self.query_one("#log-viewer-content", Static).update(self.content_text)

    def _content_text(self) -> str:
        lines = [
            "Logs",
            f"Attempt: {self.selected_attempt_number or '-'}",
            f"Tail bytes: {self.tail_bytes}",
        ]
        if self.snapshot is None:
            lines.append("No log tail loaded.")
            return "\n".join(lines)
        if self.snapshot.truncated:
            lines.append("Showing bounded tail.")
        lines.extend(
            [
                "",
                "stdout",
                self.snapshot.stdout_tail or "",
                "",
                "stderr",
                self.snapshot.stderr_tail or "",
            ]
        )
        return "\n".join(lines)


def read_bounded_log_tail(path: Path, *, tail_bytes: int) -> tuple[str, bool]:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return f"Log file missing: {path}", False
    except OSError as exc:
        return f"Log file unreadable: {path}: {exc}", False
    truncated = len(data) > tail_bytes
    if truncated:
        data = data[-tail_bytes:]
    return data.decode("utf-8", errors="replace"), truncated
