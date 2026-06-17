from __future__ import annotations

from textual.app import ComposeResult
from textual.message import Message
from textual.widgets import Static

from avarch.tui.models.profiles import ProfileRow


class ProfileCard(Static):
    class Selected(Message):
        def __init__(self, profile_name: str) -> None:
            super().__init__()
            self.profile_name = profile_name

    class DetailRequested(Message):
        def __init__(self, profile_name: str) -> None:
            super().__init__()
            self.profile_name = profile_name

    def __init__(self, profile: ProfileRow, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.profile = profile
        self.content_text = ""

    def compose(self) -> ComposeResult:
        yield Static("", id="profile-card-content")

    async def on_mount(self) -> None:
        self.render_card()

    def render_card(self) -> None:
        text = profile_card_text(self.profile)
        self.content_text = text
        if self.is_mounted:
            self.query_one("#profile-card-content", Static).update(text)

    def select(self) -> None:
        self.post_message(self.Selected(self.profile.name))

    def request_detail(self) -> None:
        self.post_message(self.DetailRequested(self.profile.name))


def profile_card_text(profile: ProfileRow) -> str:
    lines = [
        profile.name,
        f"{_origin_label(profile)} · {_vapoursynth_mode_label(profile.vapoursynth_mode)}",
    ]
    if profile.description:
        lines.extend(["", profile.description])
    lines.extend(
        [
            "",
            "Video:",
            f"  {profile.video_summary}",
            "",
            "Audio:",
            f"  {profile.audio_summary}",
            "",
            "Subtitles:",
            f"  {profile.subtitle_summary}",
        ]
    )
    if profile.tags:
        lines.extend(["", f"Tags: {', '.join(profile.tags)}"])
    if profile.known_limitations:
        lines.extend(["", "Limitations:"])
        lines.extend(f"  {limitation}" for limitation in profile.known_limitations)
    if profile.is_builtin_starting_point:
        lines.extend(["", "Reference starting point"])
    return "\n".join(lines)


def _origin_label(profile: ProfileRow) -> str:
    return "Built-in profile" if profile.origin == "builtin" else "User profile"


def _vapoursynth_mode_label(mode: str) -> str:
    if mode == "custom_template":
        return "Custom VapourSynth template"
    return "Generated VapourSynth script"
