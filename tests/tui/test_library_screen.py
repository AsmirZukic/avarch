from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session
from textual.app import App, ComposeResult

from avarch.config import AppConfig
from avarch.db import create_db_engine, create_db_schema
from avarch.models.db import MediaFile, MediaFileStatus
from avarch.probe import normalize_probe, store_probe_result
from avarch.tui.backend import LocalTuiBackend
from avarch.tui.models.library import (
    LibraryFilters,
    LibraryProbeState,
    LibraryRow,
    LibrarySnapshot,
)
from avarch.tui.models.workflow import AnalysisSummary, WorkflowDraft
from avarch.tui.screens.library import LibraryView


def test_library_lists_inventory(tmp_path: Path) -> None:
    backend, first_id, _second_id, _missing_id = _backend_with_inventory(tmp_path)

    snapshot = _run(backend.list_library())

    assert [Path(row.path).name for row in snapshot.rows] == [
        "first.mkv",
        "missing.mkv",
        "second.mkv",
    ]
    first = next(row for row in snapshot.rows if row.media_file_id == first_id)
    assert first.probe_state == LibraryProbeState.CURRENT
    assert first.video_codec == "hevc"
    assert first.resolution == "3840x2160"
    assert first.container == "matroska,webm"


def test_library_filters_probe_state() -> None:
    async def run() -> None:
        app = LibraryTestApp(_snapshot())
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.view.set_filters(
                LibraryFilters(probe_states=frozenset({LibraryProbeState.MISSING}))
            )
            assert "Second.mkv" in app.view.content_text
            assert "Movie A.mkv" not in app.view.content_text

    asyncio.run(run())


def test_library_filters_video_codec() -> None:
    async def run() -> None:
        app = LibraryTestApp(_snapshot())
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.view.set_filters(LibraryFilters(video_codec="av1"))
            assert "Already Done.mkv" in app.view.content_text
            assert "Movie A.mkv" not in app.view.content_text

    asyncio.run(run())


def test_library_shows_missing_media() -> None:
    async def run() -> None:
        app = LibraryTestApp(_snapshot())
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            assert "Missing.mkv" in app.view.content_text
            assert "missing" in app.view.content_text

    asyncio.run(run())


def test_library_analyzes_selected_media() -> None:
    refreshed = LibrarySnapshot(
        rows=(
            _row(2, "/media/Second.mkv", probe_state=LibraryProbeState.CURRENT),
        )
    )

    async def run() -> None:
        backend = FakeLibraryBackend(_snapshot(), refreshed=refreshed)
        app = LibraryTestApp(_snapshot(), backend=backend)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.view.toggle_media(2)
            await app.view.analyze_selected()
            assert backend.analyze_calls == [(2,)]
            assert "Analysis result" in app.view.content_text
            assert "current" in app.view.content_text

    asyncio.run(run())


def test_library_starts_workflow_with_selection() -> None:
    async def run() -> None:
        app = LibraryTestApp(_snapshot())
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.view.toggle_media(1)
            app.view.start_workflow_with_selection()
            await pilot.pause()
            assert app.workflow_draft is not None
            assert app.workflow_draft.selected_media_ids == {1}

    asyncio.run(run())


def test_library_does_not_enqueue_directly() -> None:
    async def run() -> None:
        backend = FakeLibraryBackend(_snapshot())
        app = LibraryTestApp(_snapshot(), backend=backend)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.view.toggle_media(1)
            app.view.start_workflow_with_selection()
            assert backend.enqueue_calls == []
            assert "Direct enqueue: disabled" in app.view.content_text

    asyncio.run(run())


class LibraryTestApp(App[None]):
    def __init__(
        self,
        snapshot: LibrarySnapshot,
        *,
        backend: FakeLibraryBackend | None = None,
    ) -> None:
        super().__init__()
        self.backend = backend or FakeLibraryBackend(snapshot)
        self.view = LibraryView(backend=self.backend, snapshot=snapshot)
        self.workflow_draft: WorkflowDraft | None = None

    def compose(self) -> ComposeResult:
        yield self.view

    def on_library_view_workflow_requested(
        self,
        event: LibraryView.WorkflowRequested,
    ) -> None:
        self.workflow_draft = event.draft


class FakeLibraryBackend:
    def __init__(
        self,
        snapshot: LibrarySnapshot,
        *,
        refreshed: LibrarySnapshot | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.refreshed = refreshed or snapshot
        self.list_calls = 0
        self.analyze_calls: list[tuple[int, ...]] = []
        self.enqueue_calls: list[tuple[int, ...]] = []

    async def list_library(self) -> LibrarySnapshot:
        self.list_calls += 1
        return self.refreshed

    async def analyze_media(self, media_file_ids: tuple[int, ...]) -> AnalysisSummary:
        self.analyze_calls.append(media_file_ids)
        return AnalysisSummary(
            requested=len(media_file_ids),
            completed=len(media_file_ids),
            failed=0,
        )


def _snapshot() -> LibrarySnapshot:
    return LibrarySnapshot(
        rows=(
            _row(1, "/media/Movie A.mkv", video_codec="hevc"),
            _row(2, "/media/Second.mkv", probe_state=LibraryProbeState.MISSING),
            _row(3, "/media/Already Done.mkv", video_codec="av1"),
            _row(
                4,
                "/media/Missing.mkv",
                inventory_status="missing",
                probe_state=LibraryProbeState.MISSING,
                selectable=False,
            ),
        )
    )


def _row(
    media_file_id: int,
    path: str,
    *,
    inventory_status: str = "present",
    probe_state: LibraryProbeState = LibraryProbeState.CURRENT,
    video_codec: str | None = "hevc",
    selectable: bool | None = None,
) -> LibraryRow:
    return LibraryRow(
        media_file_id=media_file_id,
        path=path,
        inventory_status=inventory_status,
        probe_state=probe_state,
        container="matroska,webm" if probe_state == LibraryProbeState.CURRENT else None,
        video_codec=video_codec if probe_state == LibraryProbeState.CURRENT else None,
        resolution="3840x2160" if probe_state == LibraryProbeState.CURRENT else None,
        duration_seconds=600.0 if probe_state == LibraryProbeState.CURRENT else None,
        last_scanned_at="2026-06-17T10:00:00+00:00",
        selectable=selectable if selectable is not None else inventory_status != "missing",
    )


def _backend_with_inventory(tmp_path: Path) -> tuple[LocalTuiBackend, int, int, int]:
    database_url = f"sqlite:///{tmp_path / 'avarch.db'}"
    engine = create_db_engine(database_url)
    create_db_schema(engine)
    now = datetime.now(UTC)
    first = tmp_path / "first.mkv"
    second = tmp_path / "second.mkv"
    missing = tmp_path / "missing.mkv"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    missing.write_bytes(b"missing")

    with Session(engine) as session:
        first_media = _add_media_file(session, first, now)
        second_media = _add_media_file(session, second, now)
        missing_media = _add_media_file(
            session,
            missing,
            now,
            status=MediaFileStatus.MISSING,
        )
        session.commit()
        session.refresh(first_media)
        session.refresh(second_media)
        session.refresh(missing_media)
        _add_probe(session, first_media, now)
        first_id = first_media.id or 0
        second_id = second_media.id or 0
        missing_id = missing_media.id or 0

    backend = LocalTuiBackend(
        config=AppConfig.model_validate({"database": {"url": database_url}}),
        config_path=tmp_path / "avarch.toml",
    )
    return backend, first_id, second_id, missing_id


def _add_media_file(
    session: Session,
    path: Path,
    now: datetime,
    *,
    status: MediaFileStatus = MediaFileStatus.PRESENT,
) -> MediaFile:
    stat = path.stat()
    media_file = MediaFile(
        path=str(path.resolve()),
        size_bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        device_id=stat.st_dev,
        inode=stat.st_ino,
        fs_fingerprint=f"fingerprint-{path.name}",
        discovered_at=now,
        last_seen_at=now,
        status=status,
    )
    session.add(media_file)
    session.flush()
    return media_file


def _add_probe(session: Session, media_file: MediaFile, now: datetime) -> None:
    raw_probe = _sdr_probe_payload()
    store_probe_result(
        session,
        media_file=media_file,
        raw_probe=raw_probe,
        normalized_probe=normalize_probe(raw_probe),
        created_at=now,
    )
    session.commit()
    session.refresh(media_file)


def _sdr_probe_payload() -> dict[str, Any]:
    return {
        "streams": [
            {
                "index": 0,
                "codec_name": "hevc",
                "codec_type": "video",
                "width": 3840,
                "height": 2160,
                "pix_fmt": "yuv420p10le",
                "avg_frame_rate": "24000/1001",
                "r_frame_rate": "24000/1001",
                "bits_per_raw_sample": "10",
                "color_space": "bt709",
                "color_transfer": "bt709",
                "color_primaries": "bt709",
            },
        ],
        "chapters": [],
        "format": {
            "format_name": "matroska,webm",
            "duration": "600.000000",
            "bit_rate": "8123456",
        },
    }


def _run(awaitable: Any) -> Any:
    return asyncio.run(awaitable)
