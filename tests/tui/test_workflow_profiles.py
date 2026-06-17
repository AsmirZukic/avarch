from __future__ import annotations

import asyncio
from pathlib import Path

from textual.app import App, ComposeResult

from avarch.config import AppConfig
from avarch.tui.backend import LocalTuiBackend
from avarch.tui.models.profiles import ProfileDetailSnapshot, ProfileRow, ProfileSnapshot
from avarch.tui.models.workflow import WorkflowDraft, WorkflowPreview
from avarch.tui.screens.workflow import WorkflowProfileSelectionView
from avarch.tui.widgets.profile_card import ProfileCard


def test_profile_list_shows_origin() -> None:
    async def run() -> None:
        app = ProfileSelectionTestApp(_snapshot())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert "Built-in profile" in app.view.content_text
            assert "User profile" in app.view.content_text

    asyncio.run(run())


def test_profile_list_shows_vpy_mode() -> None:
    async def run() -> None:
        app = ProfileSelectionTestApp(_snapshot())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert "Generated VapourSynth script" in app.view.content_text
            assert "Custom VapourSynth template" in app.view.content_text

    asyncio.run(run())


def test_profile_card_shows_known_limitations() -> None:
    async def run() -> None:
        app = ProfileCardTestApp(_builtin_profile())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert "Does not deinterlace." in app.card.content_text
            assert "HDR handling is limited." in app.card.content_text

    asyncio.run(run())


def test_builtin_card_calls_profile_starting_point() -> None:
    async def run() -> None:
        app = ProfileCardTestApp(_builtin_profile())
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            assert "Reference starting point" in app.card.content_text

    asyncio.run(run())


def test_selecting_profile_records_effective_hash() -> None:
    async def run() -> None:
        draft = WorkflowDraft()
        app = ProfileSelectionTestApp(_snapshot(), draft=draft)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.view.select_profile("user_film")
            assert draft.profile_name == "user_film"
            assert draft.profile_effective_hash == "user-hash"
            assert "Selected: user_film" in app.view.content_text

    asyncio.run(run())


def test_profile_change_invalidates_preview() -> None:
    async def run() -> None:
        draft = WorkflowDraft(
            profile_name="av1_1080p_sdr",
            profile_effective_hash="builtin-hash",
            preview=WorkflowPreview(
                media_file_ids=(1,),
                profile_name="av1_1080p_sdr",
                profile_effective_hash="builtin-hash",
                summary="old preview",
            ),
        )
        app = ProfileSelectionTestApp(_snapshot(), draft=draft)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.view.select_profile("user_film")
            assert draft.preview is None

    asyncio.run(run())


def test_profile_detail_opens_from_wizard() -> None:
    async def run() -> None:
        detail = _detail(_user_profile())
        backend = FakeProfileBackend(_snapshot(), detail=detail)
        app = ProfileSelectionTestApp(_snapshot(), backend=backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await app.view.open_profile_detail("user_film")
            assert backend.detail_calls == ["user_film"]
            assert "Profile detail" in app.view.content_text
            assert "definition-user" in app.view.content_text
            assert "Custom profile explanation" in app.view.content_text

    asyncio.run(run())


def test_backend_profile_snapshot_uses_resolved_profiles(tmp_path: Path) -> None:
    profile_dir = tmp_path / "profiles"
    profile_dir.mkdir()
    template_path = profile_dir / "custom.vpy"
    template_path.write_text("clip = core.std.BlankClip()\n", encoding="utf-8")
    (profile_dir / "user_film.toml").write_text(
        _profile_toml(name="user_film", extra='vapoursynth_template = "custom.vpy"\n'),
        encoding="utf-8",
    )
    backend = LocalTuiBackend(
        config=AppConfig.model_validate(
            {
                "database": {"url": f"sqlite:///{tmp_path / 'avarch.db'}"},
                "profile_registry": {"search_paths": [profile_dir]},
            }
        ),
        config_path=tmp_path / "avarch.toml",
    )

    snapshot = asyncio.run(backend.list_profiles())
    detail = asyncio.run(backend.get_profile_detail("user_film"))

    row = snapshot.profiles[0]
    assert row.origin == "user"
    assert row.vapoursynth_mode == "custom_template"
    assert row.known_limitations == ("Requires BM3D.",)
    assert len(row.effective_hash) == 64
    assert detail.profile.effective_hash == row.effective_hash


class ProfileCardTestApp(App[None]):
    def __init__(self, profile: ProfileRow) -> None:
        super().__init__()
        self.card = ProfileCard(profile)

    def compose(self) -> ComposeResult:
        yield self.card


class ProfileSelectionTestApp(App[None]):
    def __init__(
        self,
        snapshot: ProfileSnapshot,
        *,
        draft: WorkflowDraft | None = None,
        backend: FakeProfileBackend | None = None,
    ) -> None:
        super().__init__()
        self.backend = backend or FakeProfileBackend(snapshot)
        self.view = WorkflowProfileSelectionView(
            backend=self.backend,
            draft=draft or WorkflowDraft(),
        )

    def compose(self) -> ComposeResult:
        yield self.view


class FakeProfileBackend:
    def __init__(
        self,
        snapshot: ProfileSnapshot,
        *,
        detail: ProfileDetailSnapshot | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.detail = detail
        self.list_calls = 0
        self.detail_calls: list[str] = []

    async def list_profiles(self) -> ProfileSnapshot:
        self.list_calls += 1
        return self.snapshot

    async def get_profile_detail(self, profile_name: str) -> ProfileDetailSnapshot:
        self.detail_calls.append(profile_name)
        return self.detail or _detail(_builtin_profile())


def _snapshot() -> ProfileSnapshot:
    return ProfileSnapshot(profiles=(_builtin_profile(), _user_profile()))


def _builtin_profile() -> ProfileRow:
    return ProfileRow(
        name="av1_1080p_sdr",
        origin="builtin",
        description="General-purpose SDR archival profile.",
        source_path=None,
        effective_hash="builtin-hash",
        tags=("sdr", "1080p"),
        vapoursynth_mode="generated",
        video_summary="SVT-AV1 --preset 6 --crf 28; maximum width 1920.",
        audio_summary="Preferred eng audio; 2 channel libopus at 128k.",
        subtitle_summary="Subtitles: eng; keeps forced subtitles.",
        known_limitations=("Does not deinterlace.", "HDR handling is limited."),
        is_builtin_starting_point=True,
    )


def _user_profile() -> ProfileRow:
    return ProfileRow(
        name="user_film",
        origin="user",
        description="Personal film profile.",
        source_path=Path("/profiles/user_film.toml"),
        effective_hash="user-hash",
        tags=("film",),
        vapoursynth_mode="custom_template",
        video_summary="SVT-AV1 --preset 6 --crf 26; maximum width 1920.",
        audio_summary="Preferred eng audio; 2 channel libopus at 128k.",
        subtitle_summary="Subtitles: eng; keeps forced subtitles.",
        known_limitations=("Requires BM3D and fmtconv plugins.",),
    )


def _detail(profile: ProfileRow) -> ProfileDetailSnapshot:
    return ProfileDetailSnapshot(
        profile=profile,
        definition_hash=f"definition-{profile.origin}",
        vapoursynth_mode=profile.vapoursynth_mode,
        explanation=(
            "Custom profile explanation"
            if profile.origin == "user"
            else "Built-in profile explanation"
        ),
        known_limitations=profile.known_limitations,
    )


def _profile_toml(name: str, *, extra: str = "") -> str:
    return f"""
schema_version = 1
name = "{name}"
description = "User profile."
tags = ["film"]
known_limitations = ["Requires BM3D."]
{extra}
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
