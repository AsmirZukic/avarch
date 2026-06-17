from __future__ import annotations

from textual.app import ComposeResult
from textual.widgets import Static

from avarch.tui.models.jobs import JobAttemptSnapshot, JobDetailSnapshot, JobPlanSnapshot


class JobDetailView(Static):
    VALID_TABS = {"overview", "plan", "artifact", "attempts"}

    def __init__(self, snapshot: JobDetailSnapshot, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.snapshot = snapshot
        self.active_tab = "overview"
        self.selected_attempt_number = latest_attempt_number(snapshot.attempts)
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
        available_attempts = {attempt.attempt_number for attempt in snapshot.attempts}
        if self.selected_attempt_number not in available_attempts:
            self.selected_attempt_number = latest_attempt_number(snapshot.attempts)
        self.render_content()

    def select_attempt(self, attempt_number: int) -> None:
        if attempt_by_number(self.snapshot.attempts, attempt_number) is None:
            return
        self.selected_attempt_number = attempt_number
        self.active_tab = "attempts"
        self.render_content()

    def render_content(self) -> None:
        self.content_text = job_detail_text(
            self.snapshot,
            tab=self.active_tab,
            selected_attempt_number=self.selected_attempt_number,
        )
        if self.is_mounted:
            self.query_one("#job-detail-content", Static).update(self.content_text)


def job_detail_text(
    snapshot: JobDetailSnapshot,
    *,
    tab: str = "overview",
    selected_attempt_number: int | None = None,
) -> str:
    if tab == "plan":
        return job_plan_text(snapshot.plan)
    if tab == "artifact":
        return job_plan_artifact_text(snapshot.plan)
    if tab == "attempts":
        return job_attempts_text(
            snapshot.attempts,
            selected_attempt_number=selected_attempt_number,
        )
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


def job_attempts_text(
    attempts: tuple[JobAttemptSnapshot, ...],
    *,
    selected_attempt_number: int | None,
) -> str:
    ordered = ordered_attempts(attempts)
    if not ordered:
        return "Attempts\n\nNo attempts have been recorded."
    selected = attempt_by_number(
        ordered,
        selected_attempt_number or latest_attempt_number(ordered),
    )
    lines = ["Attempts", ""]
    for attempt in ordered:
        lines.append(
            f"Attempt {attempt.attempt_number}: {attempt.stage} {attempt.status} "
            f"({attempt.resource_class})"
        )
    if selected is not None:
        lines.extend(
            [
                "",
                f"Selected attempt: {selected.attempt_number}",
                f"Stage: {selected.stage}",
                f"Status: {selected.status}",
                f"Runner: {selected.runner_id}",
                f"Started: {selected.started_at.isoformat()}",
                f"Finished: {selected.finished_at.isoformat() if selected.finished_at else '-'}",
                f"Stdout: {selected.stdout_log or '-'}",
                f"Stderr: {selected.stderr_log or '-'}",
            ]
        )
        if selected.error_message is not None:
            lines.append(f"Error: {selected.error_message}")
    return "\n".join(lines)


def ordered_attempts(
    attempts: tuple[JobAttemptSnapshot, ...],
) -> tuple[JobAttemptSnapshot, ...]:
    return tuple(sorted(attempts, key=lambda attempt: attempt.attempt_number))


def latest_attempt_number(attempts: tuple[JobAttemptSnapshot, ...]) -> int | None:
    ordered = ordered_attempts(attempts)
    return ordered[-1].attempt_number if ordered else None


def attempt_by_number(
    attempts: tuple[JobAttemptSnapshot, ...],
    attempt_number: int | None,
) -> JobAttemptSnapshot | None:
    if attempt_number is None:
        return None
    for attempt in attempts:
        if attempt.attempt_number == attempt_number:
            return attempt
    return None
