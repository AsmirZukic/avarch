from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session, select
from textual.app import App, ComposeResult

from avarch.config import AppConfig
from avarch.db import create_db_engine, create_db_schema
from avarch.models.db import Job, MediaFile, MediaFileStatus
from avarch.tui.backend import LocalTuiBackend
from avarch.tui.models.workflow import (
    EnqueueResult,
    WorkflowDraft,
    WorkflowPreview,
    WorkflowPreviewRow,
)
from avarch.tui.screens.workflow import WorkflowEnqueueView


def test_enqueue_requires_current_preview() -> None:
    async def run() -> None:
        draft = WorkflowDraft(profile_effective_hash="profile-hash")
        app = EnqueueTestApp(FakeEnqueueBackend(), draft)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert "Create a current workflow preview" in app.view.content_text
            assert app.backend.enqueue_calls == []

    asyncio.run(run())


def test_confirmation_shows_job_count() -> None:
    async def run() -> None:
        app = EnqueueTestApp(FakeEnqueueBackend(), _draft_with_preview(count=3))
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert "Jobs to create: 3" in app.view.content_text

    asyncio.run(run())


def test_confirmation_states_promotion_is_manual() -> None:
    async def run() -> None:
        app = EnqueueTestApp(FakeEnqueueBackend(), _draft_with_preview())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert "Promotion remains manual" in app.view.content_text

    asyncio.run(run())


def test_stale_preview_is_rejected() -> None:
    async def run() -> None:
        draft = _draft_with_preview(profile_hash="old-hash")
        draft.profile_effective_hash = "new-hash"
        backend = FakeEnqueueBackend()
        app = EnqueueTestApp(backend, draft)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.view.confirm_enqueue()
            assert backend.enqueue_calls == []
            assert "Review the current preview" in app.view.content_text

    asyncio.run(run())


def test_confirm_calls_enqueue_once() -> None:
    async def run() -> None:
        draft = _draft_with_preview(count=2)
        backend = FakeEnqueueBackend()
        app = EnqueueTestApp(backend, draft)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.view.confirm_enqueue()
            assert backend.enqueue_calls == [((1, 2), "av1_1080p_sdr", 7)]

    asyncio.run(run())


def test_cancel_creates_no_jobs() -> None:
    async def run() -> None:
        backend = FakeEnqueueBackend()
        app = EnqueueTestApp(backend, _draft_with_preview())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.view.cancel()
            assert backend.enqueue_calls == []
            assert "Canceled" in app.view.content_text

    asyncio.run(run())


def test_success_resets_workflow_draft() -> None:
    async def run() -> None:
        draft = _draft_with_preview()
        app = EnqueueTestApp(FakeEnqueueBackend(), draft)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.view.confirm_enqueue()
            assert draft.selected_media_ids == set()
            assert draft.profile_name is None
            assert draft.profile_effective_hash is None
            assert draft.preview is None
            assert draft.priority == 0

    asyncio.run(run())


def test_success_can_open_queue() -> None:
    async def run() -> None:
        app = EnqueueTestApp(FakeEnqueueBackend(), _draft_with_preview())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.view.open_queue()
            await pilot.pause()
            assert app.open_queue_requests == 1

    asyncio.run(run())


def test_backend_enqueue_creates_selected_jobs_only(tmp_path: Path) -> None:
    backend, first_id, _second_id = _backend_with_media(tmp_path)

    result = asyncio.run(
        backend.enqueue_workflow(
            media_file_ids=(first_id,),
            profile_name="enqueue_profile",
            priority=4,
        )
    )

    engine = create_db_engine(backend.database_url)
    with Session(engine) as session:
        jobs = list(session.exec(select(Job)).all())

    assert result.created == 1
    assert len(jobs) == 1
    assert jobs[0].media_file_id == first_id
    assert jobs[0].priority == 4


class EnqueueTestApp(App[None]):
    def __init__(self, backend: FakeEnqueueBackend, draft: WorkflowDraft) -> None:
        super().__init__()
        self.backend = backend
        self.view = WorkflowEnqueueView(backend=backend, draft=draft)
        self.open_queue_requests = 0

    def compose(self) -> ComposeResult:
        yield self.view

    def on_workflow_enqueue_view_open_queue_requested(
        self,
        _event: WorkflowEnqueueView.OpenQueueRequested,
    ) -> None:
        self.open_queue_requests += 1


class FakeEnqueueBackend:
    def __init__(self, result: EnqueueResult | None = None) -> None:
        self.result = result or EnqueueResult(
            created=1,
            already_queued=0,
            already_completed=0,
            not_eligible=0,
        )
        self.enqueue_calls: list[tuple[tuple[int, ...], str, int]] = []

    async def enqueue_workflow(
        self,
        *,
        media_file_ids: tuple[int, ...],
        profile_name: str,
        priority: int,
    ) -> EnqueueResult:
        self.enqueue_calls.append((media_file_ids, profile_name, priority))
        return self.result


def _draft_with_preview(
    *,
    count: int = 1,
    profile_hash: str = "profile-hash",
) -> WorkflowDraft:
    media_file_ids = tuple(range(1, count + 1))
    return WorkflowDraft(
        selected_media_ids=set(media_file_ids),
        profile_name="av1_1080p_sdr",
        profile_effective_hash=profile_hash,
        preview=WorkflowPreview(
            media_file_ids=media_file_ids,
            profile_name="av1_1080p_sdr",
            profile_effective_hash=profile_hash,
            summary="This workflow will wait for manual promotion.",
            rows=tuple(
                WorkflowPreviewRow(
                    media_file_id=media_file_id,
                    path=f"/media/{media_file_id}.mkv",
                    result="ENCODE",
                    video="HEVC -> AV1",
                    audio="eng -> libopus",
                    subtitles="Keep eng",
                    reason="Ready.",
                )
                for media_file_id in media_file_ids
            ),
        ),
        priority=7,
    )


def _backend_with_media(tmp_path: Path) -> tuple[LocalTuiBackend, int, int]:
    database_url = f"sqlite:///{tmp_path / 'avarch.db'}"
    engine = create_db_engine(database_url)
    create_db_schema(engine)
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "enqueue_profile.toml").write_text(_profile_toml(), encoding="utf-8")
    now = datetime.now(UTC)
    first = tmp_path / "first.mkv"
    second = tmp_path / "second.mkv"
    first.write_bytes(b"first")
    second.write_bytes(b"second")

    with Session(engine) as session:
        first_media = _add_media_file(session, first, now)
        second_media = _add_media_file(session, second, now)
        session.commit()
        session.refresh(first_media)
        session.refresh(second_media)
        first_id = first_media.id or 0
        second_id = second_media.id or 0

    backend = LocalTuiBackend(
        config=AppConfig.model_validate(
            {
                "database": {"url": database_url},
                "profile_registry": {"search_paths": [profile_dir]},
            }
        ),
        config_path=tmp_path / "avarch.toml",
    )
    return backend, first_id, second_id


def _add_media_file(session: Session, path: Path, now: datetime) -> MediaFile:
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
        status=MediaFileStatus.PRESENT,
    )
    session.add(media_file)
    session.flush()
    return media_file


def _profile_toml() -> str:
    return """
schema_version = 1
name = "enqueue_profile"

backend = "av1an"
container = "mkv"

[match]
video_codec_not = ["av1"]

[video]
max_width = 1920
hdr_to_sdr = true
source = "vapoursynth"

[av1an]
encoder = "svt-av1"
workers = 2
video_args = "--preset 6 --crf 28"

[audio]
codec = "libopus"
bitrate = "128k"
channels = 2
languages = ["eng"]

[subtitles]
languages = ["eng"]
keep_forced = true
"""
