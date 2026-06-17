from __future__ import annotations

import asyncio
from pathlib import Path

from avarch.tui.models.jobs import JobLogSnapshot
from avarch.tui.widgets.log_viewer import LogViewer, read_bounded_log_tail


def test_log_viewer_defaults_to_latest_attempt() -> None:
    async def run() -> None:
        backend = FakeLogBackend()
        viewer = LogViewer(
            backend=backend,
            job_id=7,
            attempt_numbers=(1, 3, 2),
            running_attempt_numbers=frozenset(),
            tail_bytes=128,
        )

        await viewer.load()

        assert viewer.selected_attempt_number == 3
        assert backend.calls == [(7, 3, 128)]

    asyncio.run(run())


def test_log_viewer_loads_bounded_tail(tmp_path: Path) -> None:
    log_path = tmp_path / "stdout.log"
    log_path.write_text("0123456789", encoding="utf-8")

    tail, truncated = read_bounded_log_tail(log_path, tail_bytes=4)

    assert tail == "6789"
    assert truncated is True


def test_log_viewer_handles_invalid_utf8(tmp_path: Path) -> None:
    log_path = tmp_path / "stderr.log"
    log_path.write_bytes(b"ok\xffdone")

    tail, truncated = read_bounded_log_tail(log_path, tail_bytes=32)

    assert tail == "ok\ufffddone"
    assert truncated is False


def test_log_viewer_handles_missing_file(tmp_path: Path) -> None:
    tail, truncated = read_bounded_log_tail(tmp_path / "missing.log", tail_bytes=32)

    assert "Log file missing" in tail
    assert truncated is False


def test_running_log_can_auto_refresh() -> None:
    async def run() -> None:
        backend = FakeLogBackend()
        viewer = LogViewer(
            backend=backend,
            job_id=7,
            attempt_numbers=(2,),
            running_attempt_numbers=frozenset({2}),
        )

        await viewer.maybe_auto_refresh()

        assert backend.calls == [(7, 2, 65536)]

    asyncio.run(run())


def test_terminal_log_does_not_refresh_unnecessarily() -> None:
    async def run() -> None:
        backend = FakeLogBackend()
        viewer = LogViewer(
            backend=backend,
            job_id=7,
            attempt_numbers=(2,),
            running_attempt_numbers=frozenset(),
        )

        await viewer.maybe_auto_refresh()

        assert backend.calls == []

    asyncio.run(run())


class FakeLogBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[int, int | None, int]] = []

    async def get_job_log_tail(
        self,
        *,
        job_id: int,
        attempt_number: int | None,
        tail_bytes: int,
    ) -> JobLogSnapshot:
        self.calls.append((job_id, attempt_number, tail_bytes))
        return JobLogSnapshot(
            job_id=job_id,
            attempt_number=attempt_number,
            tail_bytes=tail_bytes,
            stdout_tail="stdout",
            stderr_tail="stderr",
            truncated=False,
        )
