from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session, select
from textual.app import App, ComposeResult

from avarch.config import AppConfig
from avarch.db import create_db_engine, create_db_schema
from avarch.models.db import Job, MediaFile, MediaFileStatus
from avarch.probe import normalize_probe, store_probe_result
from avarch.tui.backend import LocalTuiBackend
from avarch.tui.models.workflow import WorkflowDraft, WorkflowPreview, WorkflowPreviewRow
from avarch.tui.screens.workflow import WorkflowPreviewView
from avarch.tui.widgets.plan_summary import plan_summary_text


def test_preview_calls_pure_backend_preview() -> None:
    async def run() -> None:
        backend = FakePreviewBackend(_preview())
        draft = WorkflowDraft(
            selected_media_ids={2, 1},
            profile_name="av1_1080p_sdr",
            profile_effective_hash="profile-hash",
        )
        app = PreviewTestApp(backend, draft)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            await app.view.generate_preview()
            assert backend.preview_calls == [((1, 2), "av1_1080p_sdr")]
            assert draft.preview is backend.preview

    asyncio.run(run())


def test_preview_creates_no_jobs(tmp_path: Path) -> None:
    backend, first_id, _second_id = _backend_with_media(tmp_path, second_probe=False)

    _run(backend.preview_workflow(media_file_ids=(first_id,), profile_name="preview_profile"))

    engine = create_db_engine(backend.database_url)
    with Session(engine) as session:
        jobs = list(session.exec(select(Job)).all())
    assert jobs == []


def test_preview_writes_no_artifacts(tmp_path: Path) -> None:
    backend, first_id, _second_id = _backend_with_media(tmp_path, second_probe=False)

    _run(backend.preview_workflow(media_file_ids=(first_id,), profile_name="preview_profile"))

    assert not backend.data_dir.exists()


def test_preview_explains_every_pipeline_stage() -> None:
    text = plan_summary_text(_preview())

    assert "Create in-memory encode plans" in text
    assert "Encode video" in text
    assert "audio policy" in text
    assert "Validate duration" in text
    assert "manual approval" in text


def test_preview_shows_video_audio_and_subtitles(tmp_path: Path) -> None:
    backend, first_id, _second_id = _backend_with_media(tmp_path, second_probe=False)

    preview = _run(
        backend.preview_workflow(media_file_ids=(first_id,), profile_name="preview_profile")
    )

    row = preview.rows[0]
    assert row.result == "ENCODE"
    assert "3840x2160 -> 1920x1080" in row.video
    assert "libopus" in row.audio
    assert "Keep eng forced" in row.subtitles


def test_preview_shows_manual_promotion_requirement() -> None:
    text = plan_summary_text(_preview())

    assert "manual approval before promotion" in text


def test_preview_shows_blocked_files() -> None:
    preview = WorkflowPreview(
        media_file_ids=(1,),
        profile_name="av1_1080p_sdr",
        profile_effective_hash="profile-hash",
        summary="This workflow will:\nBlocked: 1",
        rows=(
            WorkflowPreviewRow(
                media_file_id=1,
                path="/media/Movie D.mkv",
                result="BLOCKED",
                video="No plan",
                audio="No plan",
                subtitles="No plan",
                reason="Profile does not apply.",
            ),
        ),
    )

    text = plan_summary_text(preview)

    assert "BLOCKED" in text
    assert "Profile does not apply." in text


def test_preview_requires_analysis_for_missing_metadata(tmp_path: Path) -> None:
    backend, _first_id, second_id = _backend_with_media(tmp_path, second_probe=False)

    preview = _run(
        backend.preview_workflow(media_file_ids=(second_id,), profile_name="preview_profile")
    )

    assert preview.rows[0].result == "NEEDS ANALYSIS"
    assert "No current probe" in preview.rows[0].reason


def test_profile_hash_change_invalidates_preview() -> None:
    async def run() -> None:
        draft = WorkflowDraft(
            selected_media_ids={1},
            profile_name="av1_1080p_sdr",
            profile_effective_hash="new-profile-hash",
            preview=_preview(profile_hash="old-profile-hash"),
        )
        app = PreviewTestApp(FakePreviewBackend(_preview()), draft)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            assert draft.preview is None
            assert "Profile behavior changed" in app.view.content_text

    asyncio.run(run())


class PreviewTestApp(App[None]):
    def __init__(self, backend: FakePreviewBackend, draft: WorkflowDraft) -> None:
        super().__init__()
        self.view = WorkflowPreviewView(backend=backend, draft=draft)

    def compose(self) -> ComposeResult:
        yield self.view


class FakePreviewBackend:
    def __init__(self, preview: WorkflowPreview) -> None:
        self.preview = preview
        self.preview_calls: list[tuple[tuple[int, ...], str]] = []

    async def preview_workflow(
        self,
        *,
        media_file_ids: tuple[int, ...],
        profile_name: str,
    ) -> WorkflowPreview:
        self.preview_calls.append((media_file_ids, profile_name))
        return self.preview


def _backend_with_media(
    tmp_path: Path,
    *,
    second_probe: bool,
) -> tuple[LocalTuiBackend, int, int]:
    database_url = f"sqlite:///{tmp_path / 'avarch.db'}"
    engine = create_db_engine(database_url)
    create_db_schema(engine)
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    (profile_dir / "preview_profile.toml").write_text(_profile_toml(), encoding="utf-8")
    first = tmp_path / "first.mkv"
    second = tmp_path / "second.mkv"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    now = datetime.now(UTC)

    with Session(engine) as session:
        first_media = _add_media_file(session, first, now)
        second_media = _add_media_file(session, second, now)
        session.commit()
        session.refresh(first_media)
        session.refresh(second_media)
        _add_probe(session, first_media, now)
        if second_probe:
            _add_probe(session, second_media, now)
        first_id = first_media.id or 0
        second_id = second_media.id or 0

    backend = LocalTuiBackend(
        config=AppConfig.model_validate(
            {
                "app": {"data_dir": tmp_path / ".avarch"},
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


def _preview(*, profile_hash: str = "profile-hash") -> WorkflowPreview:
    return WorkflowPreview(
        media_file_ids=(1,),
        profile_name="av1_1080p_sdr",
        profile_effective_hash=profile_hash,
        summary="\n".join(
            [
                "This workflow will:",
                "1. Create in-memory encode plans for 1 file(s).",
                "2. Encode video to 10-bit AV1.",
                "3. Apply the profile audio policy and mux selected subtitles.",
                "4. Validate duration, streams, codec, resolution, and output size.",
                "5. Wait for your manual approval before promotion.",
            ]
        ),
        rows=(
            WorkflowPreviewRow(
                media_file_id=1,
                path="/media/Movie A.mkv",
                result="ENCODE",
                video="3840x2160 -> 1920x1080 HEVC -> AV1",
                audio="eng eac3 -> libopus 2ch 128k",
                subtitles="Keep eng forced",
                reason="Plan can be created with the selected profile.",
            ),
        ),
    )


def _profile_toml() -> str:
    return """
schema_version = 1
name = "preview_profile"
description = "Preview test profile."

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
            {
                "index": 1,
                "codec_name": "eac3",
                "codec_type": "audio",
                "channels": 6,
                "tags": {"language": "eng", "title": "Main Audio"},
                "disposition": {"default": 1, "forced": 0},
            },
            {
                "index": 2,
                "codec_name": "subrip",
                "codec_type": "subtitle",
                "tags": {"language": "eng", "title": "Forced"},
                "disposition": {"default": 0, "forced": 1},
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
