from __future__ import annotations

from textual.app import ComposeResult
from textual.widgets import Static

from avarch.tui.models.jobs import JobDetailSnapshot, JobPlanSnapshot


class JobDetailView(Static):
    VALID_TABS = {"overview", "plan", "artifact"}

    def __init__(self, snapshot: JobDetailSnapshot, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.snapshot = snapshot
        self.active_tab = "overview"
        self.plan_artifact_editable = False
        self.content_text = job_detail_text(snapshot, tab=self.active_tab)

    def compose(self) -> ComposeResult:
        yield Static(self.content_text, id="job-detail-content")

    async def on_mount(self) -> None:
        self.render_content()

    def select_tab(self, tab: str) -> None:
        if tab not in self.VALID_TABS:
            raise ValueError(f"Unknown job detail tab: {tab}")
        self.active_tab = tab
        self.render_content()

    def update_snapshot(self, snapshot: JobDetailSnapshot) -> None:
        self.snapshot = snapshot
        self.render_content()

    def render_content(self) -> None:
        self.content_text = job_detail_text(self.snapshot, tab=self.active_tab)
        if self.is_mounted:
            self.query_one("#job-detail-content", Static).update(self.content_text)


def job_detail_text(snapshot: JobDetailSnapshot, *, tab: str = "overview") -> str:
    if tab == "plan":
        return job_plan_text(snapshot.plan)
    if tab == "artifact":
        return job_plan_artifact_text(snapshot.plan)
    return job_overview_text(snapshot)


def job_overview_text(snapshot: JobDetailSnapshot) -> str:
    lines = [
        f"Job {snapshot.job_id}: {snapshot.file_name}",
        "",
        f"Status: {snapshot.status}",
        f"Stage: {snapshot.stage}",
        f"Priority: {snapshot.priority}",
        f"Attempts: {snapshot.attempts_count}",
        f"Source: {snapshot.source_path}",
        f"Output: {snapshot.output_path or '-'}",
        "",
        f"Profile: {snapshot.profile_name}",
        f"Profile hash: {snapshot.profile_hash}",
        f"Queue key: {snapshot.queue_key}",
        f"Source fingerprint: {snapshot.source_fs_fingerprint}",
        f"Probe hash: {snapshot.probe_hash or '-'}",
        f"Plan hash: {snapshot.plan_hash or '-'}",
    ]
    if snapshot.control_request is not None:
        lines.extend(["", f"Control: {snapshot.control_request}"])
        if snapshot.control_reason is not None:
            lines.append(f"Reason: {snapshot.control_reason}")
    if snapshot.last_error_message is not None:
        lines.extend(["", f"Last error: {snapshot.last_error_message}"])
    return "\n".join(lines)


def job_plan_text(plan: JobPlanSnapshot | None) -> str:
    if plan is None:
        return "Plan\n\nNo plan artifact is available for this job."
    subtitles = (
        ", ".join(str(index) for index in plan.subtitle_stream_indexes)
        if plan.subtitle_stream_indexes
        else "-"
    )
    audio_language = (
        f" ({plan.validation_expected_audio_language})"
        if plan.validation_expected_audio_language
        else ""
    )
    decode_sample = "enabled" if plan.validation_decode_sample else "disabled"
    lines = [
        "Plan",
        "",
        f"Plan hash: {plan.plan_hash}",
        f"Plan path: {plan.plan_path}",
        "",
        f"Video stream: {plan.video_stream_index}",
        f"Audio stream: {plan.audio_stream_index}",
        f"Subtitle streams: {subtitles}",
        "",
        f"VapourSynth mode: {plan.vapoursynth_mode}",
        f"VapourSynth identity: {plan.vapoursynth_identity_hash}",
        f"VapourSynth template: {plan.vapoursynth_template_path or '-'}",
        f"VapourSynth template hash: {plan.vapoursynth_template_hash or '-'}",
        "",
        f"Validation policy: {plan.validation_policy_hash}",
        "Expected video: "
        f"{plan.validation_expected_video_codec} "
        f"{plan.validation_expected_width}x{plan.validation_expected_height}",
        "Expected audio: "
        f"{plan.validation_expected_audio_codec} "
        f"{plan.validation_expected_audio_channels}ch{audio_language}",
        f"Decode sample: {decode_sample}",
    ]
    return "\n".join(lines)


def job_plan_artifact_text(plan: JobPlanSnapshot | None) -> str:
    if plan is None:
        return "Read-only plan artifact\n\nNo plan artifact is available for this job."
    return "\n".join(
        [
            "Read-only plan artifact",
            "",
            f"Path: {plan.plan_path}",
            "",
            plan.artifact_text,
        ]
    )
