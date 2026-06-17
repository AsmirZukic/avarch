from __future__ import annotations

from pathlib import Path
from typing import Protocol

from textual.app import ComposeResult
from textual.message import Message
from textual.widgets import Button, Static

from avarch.tui.models.profiles import ProfileDetailSnapshot, ProfileRow, ProfileSnapshot
from avarch.tui.models.workflow import (
    AnalysisSummary,
    CandidateSnapshot,
    DirectoryListing,
    ScanSummary,
    WorkflowDraft,
    WorkflowPreview,
)
from avarch.tui.widgets.candidate_table import CandidateTable
from avarch.tui.widgets.file_browser import FileBrowser
from avarch.tui.widgets.plan_summary import PlanSummary, plan_summary_text
from avarch.tui.widgets.profile_card import ProfileCard, profile_card_text


class WorkflowScanBackend(Protocol):
    async def browse_directory(self, path: Path, *, show_hidden: bool) -> DirectoryListing:
        ...

    async def scan_roots(self, roots: tuple[Path, ...]) -> ScanSummary:
        ...

    async def list_workflow_candidates(self, roots: tuple[Path, ...]) -> CandidateSnapshot:
        ...


class WorkflowCandidateBackend(Protocol):
    async def list_workflow_candidates(self, roots: tuple[Path, ...]) -> CandidateSnapshot:
        ...

    async def analyze_media(self, media_file_ids: tuple[int, ...]) -> AnalysisSummary:
        ...


class WorkflowProfileBackend(Protocol):
    async def list_profiles(self) -> ProfileSnapshot:
        ...

    async def get_profile_detail(self, profile_name: str) -> ProfileDetailSnapshot:
        ...


class WorkflowPreviewBackend(Protocol):
    async def preview_workflow(
        self,
        *,
        media_file_ids: tuple[int, ...],
        profile_name: str,
    ) -> WorkflowPreview:
        ...


class WorkflowScanView(Static):
    class Canceled(Message):
        pass

    class ScanCompleted(Message):
        def __init__(self, summary: ScanSummary) -> None:
            super().__init__()
            self.summary = summary

    DEFAULT_CSS = """
    WorkflowScanView {
        height: 1fr;
        padding: 1;
    }

    WorkflowScanView Button {
        margin-right: 1;
        margin-top: 1;
    }
    """

    def __init__(
        self,
        *,
        backend: WorkflowScanBackend,
        draft: WorkflowDraft | None = None,
        start_path: Path | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.backend = backend
        self.draft = draft or WorkflowDraft()
        self.browser = FileBrowser(backend=backend, start_path=start_path)
        self.phase = "Select folders"
        self.error_message: str | None = None
        self.content_text = ""

    def compose(self) -> ComposeResult:
        yield Static("", id="workflow-scan-status")
        yield self.browser
        yield Button("Scan selected folders", id="workflow-scan")
        yield Button("Cancel", id="workflow-cancel")

    async def on_mount(self) -> None:
        self._render_status()

    async def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "workflow-scan":
            await self.run_scan()
            event.stop()
        elif event.button.id == "workflow-cancel":
            self.cancel()
            event.stop()

    def on_file_browser_selection_changed(self, event: FileBrowser.SelectionChanged) -> None:
        self.draft.roots = self.browser.normalized_selected_roots()
        self._render_status()
        event.stop()

    async def run_scan(self) -> None:
        roots = self.browser.normalized_selected_roots()
        self.draft.roots = roots
        if not roots:
            self.error_message = "Select at least one valid folder before scanning."
            self.phase = "Selection required"
            self._render_status()
            return

        self.error_message = None
        self.phase = f"Scanning {len(roots)} folder(s)"
        self._render_status()
        try:
            summary = await self.backend.scan_roots(roots)
        except Exception as exc:
            self.error_message = str(exc) or exc.__class__.__name__
            self.phase = "Scan failed"
            self._render_status()
            return

        self.draft.scan_summary = summary
        self.phase = "Scan complete"
        self._render_status()
        self.post_message(self.ScanCompleted(summary))

    def cancel(self) -> None:
        self.draft = WorkflowDraft()
        self.error_message = None
        self.phase = "Canceled"
        self._render_status()
        self.post_message(self.Canceled())

    def _render_status(self) -> None:
        text = self._status_text()
        self.content_text = text
        if self.is_mounted:
            self.query_one("#workflow-scan-status", Static).update(text)

    def _status_text(self) -> str:
        lines = [
            "New Workflow - Scan",
            "",
            "Scanning updates Avarch's media inventory.",
            "It reads directory entries and file metadata.",
            "It does not encode, rename, replace, or delete media.",
            "",
            f"Phase: {self.phase}",
            f"Selected folders: {len(self.draft.roots)}",
        ]
        if self.error_message is not None:
            lines.extend(["", f"Error: {self.error_message}"])
        if self.draft.scan_summary is not None:
            summary = self.draft.scan_summary
            lines.extend(
                [
                    "",
                    "Scan result",
                    f"  Roots scanned: {len(summary.roots)}",
                    f"  New: {summary.added}",
                    f"  Changed: {summary.changed}",
                    f"  Missing: {summary.missing}",
                    f"  Unchanged: {summary.unchanged}",
                ]
            )
        return "\n".join(lines)


class WorkflowCandidateReviewView(Static):
    class ContinueRequested(Message):
        def __init__(self, media_file_ids: tuple[int, ...]) -> None:
            super().__init__()
            self.media_file_ids = media_file_ids

    DEFAULT_CSS = """
    WorkflowCandidateReviewView {
        height: 1fr;
        padding: 1;
    }

    WorkflowCandidateReviewView Button {
        margin-right: 1;
        margin-top: 1;
    }
    """

    def __init__(
        self,
        *,
        snapshot: CandidateSnapshot,
        backend: WorkflowCandidateBackend | None = None,
        draft: WorkflowDraft | None = None,
        roots: tuple[Path, ...] = (),
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.backend = backend
        self.roots = roots
        self.draft = draft or WorkflowDraft()
        self.table = CandidateTable(
            snapshot,
            selected_media_ids=set(self.draft.selected_media_ids),
            id="candidate-table",
        )
        self.analysis_summary: AnalysisSummary | None = None
        self.analysis_phase = "Idle"
        self.error_message: str | None = None
        self.content_text = ""

    def compose(self) -> ComposeResult:
        yield Static("", id="candidate-review-status")
        yield self.table
        yield Button("Select all eligible", id="candidate-select-all")
        yield Button("Clear selection", id="candidate-clear-selection")
        yield Button("Analyze selected", id="candidate-analyze")
        yield Button("Continue", id="candidate-continue")

    async def on_mount(self) -> None:
        self._sync_selection()
        self._render_status()

    def on_candidate_table_selection_changed(
        self,
        event: CandidateTable.SelectionChanged,
    ) -> None:
        self.draft.selected_media_ids = set(event.media_file_ids)
        self._render_status()
        event.stop()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "candidate-select-all":
            self.table.select_all_eligible()
            event.stop()
        elif event.button.id == "candidate-clear-selection":
            self.table.clear_selection()
            event.stop()
        elif event.button.id == "candidate-analyze":
            self.run_worker(self.analyze_selected(), exclusive=True)
            event.stop()
        elif event.button.id == "candidate-continue":
            self._sync_selection()
            self.post_message(
                self.ContinueRequested(tuple(sorted(self.draft.selected_media_ids)))
            )
            event.stop()

    async def analyze_selected(self) -> None:
        self._sync_selection()
        selected_ids = tuple(sorted(self.draft.selected_media_ids))
        if not selected_ids:
            self.error_message = "Select at least one candidate before analysis."
            self.analysis_phase = "Selection required"
            self._render_status()
            return
        if self.backend is None:
            self.error_message = "Analysis backend is not available."
            self.analysis_phase = "Analysis unavailable"
            self._render_status()
            return

        self.error_message = None
        self.analysis_summary = None
        self.analysis_phase = f"Analyzing {len(selected_ids)} files"
        self._render_status()
        summary = await self.backend.analyze_media(selected_ids)
        self.analysis_summary = summary
        self.analysis_phase = (
            f"{summary.completed} / {summary.requested} complete"
            if summary.failed
            else "Analysis complete"
        )

        refreshed = await self.backend.list_workflow_candidates(self.roots)
        self.table.update_snapshot(refreshed)
        self._sync_selection()
        self._render_status()

    def _sync_selection(self) -> None:
        self.draft.selected_media_ids = set(self.table.selected_media_ids)

    def _render_status(self) -> None:
        selected = len(self.draft.selected_media_ids)
        eligible = sum(1 for row in self.table.snapshot.rows if row.eligible)
        total = len(self.table.snapshot.rows)
        lines = [
            "New Workflow - Review candidates",
            "",
            f"Discovered media: {total}",
            f"Eligible: {eligible}",
            f"Selected: {selected}",
            f"Analysis: {self.analysis_phase}",
        ]
        if self.error_message is not None:
            lines.extend(["", f"Error: {self.error_message}"])
        if self.analysis_summary is not None:
            summary = self.analysis_summary
            lines.extend(
                [
                    "",
                    "Analysis result",
                    f"  Requested: {summary.requested}",
                    f"  Completed: {summary.completed}",
                    f"  Failed: {summary.failed}",
                ]
            )
            for failure in summary.failures:
                lines.append(f"  {failure.path}: {failure.error}")
        text = "\n".join(lines)
        self.content_text = text
        if self.is_mounted:
            self.query_one("#candidate-review-status", Static).update(text)


class WorkflowProfileSelectionView(Static):
    class ProfileSelected(Message):
        def __init__(self, profile_name: str, effective_hash: str) -> None:
            super().__init__()
            self.profile_name = profile_name
            self.effective_hash = effective_hash

    class ProfileDetailOpened(Message):
        def __init__(self, detail: ProfileDetailSnapshot) -> None:
            super().__init__()
            self.detail = detail

    class OpenProfilesRequested(Message):
        pass

    DEFAULT_CSS = """
    WorkflowProfileSelectionView {
        height: 1fr;
        padding: 1;
    }

    WorkflowProfileSelectionView Button {
        margin-right: 1;
        margin-top: 1;
    }
    """

    def __init__(
        self,
        *,
        backend: WorkflowProfileBackend,
        draft: WorkflowDraft | None = None,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.backend = backend
        self.draft = draft or WorkflowDraft()
        self.snapshot = ProfileSnapshot(profiles=())
        self.detail: ProfileDetailSnapshot | None = None
        self.phase = "Loading profiles"
        self.error_message: str | None = None
        self.content_text = ""
        self.profile_list_text = ""

    def compose(self) -> ComposeResult:
        yield Static("", id="profile-selection-status")
        yield Static("", id="profile-selection-list")
        yield Button("Open Profiles", id="profile-open-profiles")

    async def on_mount(self) -> None:
        await self.load_profiles()

    async def load_profiles(self) -> None:
        self.phase = "Loading profiles"
        self.error_message = None
        self._render_selection()
        try:
            self.snapshot = await self.backend.list_profiles()
        except Exception as exc:
            self.phase = "Profile load failed"
            self.error_message = str(exc) or exc.__class__.__name__
            self._render_selection()
            return
        self.phase = "Choose a profile"
        self._render_selection()

    async def on_profile_card_selected(self, event: ProfileCard.Selected) -> None:
        await self.select_profile(event.profile_name)
        event.stop()

    async def on_profile_card_detail_requested(
        self,
        event: ProfileCard.DetailRequested,
    ) -> None:
        await self.open_profile_detail(event.profile_name)
        event.stop()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "profile-open-profiles":
            self.post_message(self.OpenProfilesRequested())
            event.stop()

    async def select_profile(self, profile_name: str) -> None:
        row = self._profile_by_name(profile_name)
        if row is None:
            self.phase = "Profile unavailable"
            self.error_message = f"Profile not found: {profile_name}"
            self._render_selection()
            return

        changed = (
            self.draft.profile_name != row.name
            or self.draft.profile_effective_hash != row.effective_hash
        )
        self.draft.profile_name = row.name
        self.draft.profile_effective_hash = row.effective_hash
        if changed:
            self.draft.preview = None
        self.phase = "Profile selected"
        self.error_message = None
        self._render_selection()
        self.post_message(self.ProfileSelected(row.name, row.effective_hash))

    async def open_profile_detail(self, profile_name: str) -> None:
        try:
            self.detail = await self.backend.get_profile_detail(profile_name)
        except Exception as exc:
            self.phase = "Profile detail failed"
            self.error_message = str(exc) or exc.__class__.__name__
            self._render_selection()
            return
        self.phase = "Profile detail"
        self.error_message = None
        self._render_selection()
        self.post_message(self.ProfileDetailOpened(self.detail))

    def _profile_by_name(self, profile_name: str) -> ProfileRow | None:
        for row in self.snapshot.profiles:
            if row.name == profile_name:
                return row
        return None

    def _render_selection(self) -> None:
        status = self._status_text()
        list_text = self._list_text()
        self.content_text = "\n\n".join(part for part in (status, list_text) if part)
        self.profile_list_text = list_text
        if self.is_mounted:
            self.query_one("#profile-selection-status", Static).update(status)
            self.query_one("#profile-selection-list", Static).update(list_text)

    def _status_text(self) -> str:
        lines = [
            "New Workflow - Select profile",
            "",
            f"Phase: {self.phase}",
            f"Profiles: {len(self.snapshot.profiles)}",
            f"Selected: {self.draft.profile_name or 'none'}",
        ]
        if self.error_message is not None:
            lines.extend(["", f"Error: {self.error_message}"])
        if self.detail is not None:
            detail = self.detail
            lines.extend(
                [
                    "",
                    "Profile detail",
                    f"  Name: {detail.profile.name}",
                    f"  Definition hash: {detail.definition_hash}",
                    f"  Effective hash: {detail.profile.effective_hash}",
                    f"  VapourSynth: {detail.vapoursynth_mode}",
                    f"  {detail.explanation}",
                ]
            )
        return "\n".join(lines)

    def _list_text(self) -> str:
        if not self.snapshot.profiles:
            return "No profiles are available."
        rendered: list[str] = []
        for row in self.snapshot.profiles:
            prefix = "[selected]\n" if row.name == self.draft.profile_name else ""
            rendered.append(prefix + profile_card_text(row))
        return "\n\n---\n\n".join(rendered)


class WorkflowPreviewView(Static):
    class PreviewReady(Message):
        def __init__(self, preview: WorkflowPreview) -> None:
            super().__init__()
            self.preview = preview

    DEFAULT_CSS = """
    WorkflowPreviewView {
        height: 1fr;
        padding: 1;
    }

    WorkflowPreviewView Button {
        margin-right: 1;
        margin-top: 1;
    }
    """

    def __init__(
        self,
        *,
        backend: WorkflowPreviewBackend,
        draft: WorkflowDraft,
        id: str | None = None,
    ) -> None:
        super().__init__(id=id)
        self.backend = backend
        self.draft = draft
        self.phase = "Preview required"
        self.error_message: str | None = None
        self.content_text = ""
        self.summary_widget: PlanSummary | None = None

    def compose(self) -> ComposeResult:
        yield Static("", id="workflow-preview-status")
        yield Static("", id="workflow-preview-summary")
        yield Button("Create preview", id="workflow-preview-create")

    async def on_mount(self) -> None:
        self.ensure_preview_current()
        self._render_preview()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "workflow-preview-create":
            self.run_worker(self.generate_preview(), exclusive=True)
            event.stop()

    async def generate_preview(self) -> None:
        selected_ids = tuple(sorted(self.draft.selected_media_ids))
        if not selected_ids:
            self.phase = "Selection required"
            self.error_message = "Select at least one candidate before preview."
            self._render_preview()
            return
        if self.draft.profile_name is None:
            self.phase = "Profile required"
            self.error_message = "Select a profile before preview."
            self._render_preview()
            return

        self.phase = "Creating preview"
        self.error_message = None
        self._render_preview()
        preview = await self.backend.preview_workflow(
            media_file_ids=selected_ids,
            profile_name=self.draft.profile_name,
        )
        if (
            self.draft.profile_effective_hash is not None
            and preview.profile_effective_hash != self.draft.profile_effective_hash
        ):
            self.draft.preview = None
            self.phase = "Preview stale"
            self.error_message = "Profile behavior changed. Review the profile again."
            self._render_preview()
            return

        self.draft.preview = preview
        self.phase = "Preview ready"
        self.error_message = None
        self._render_preview()
        self.post_message(self.PreviewReady(preview))

    def ensure_preview_current(self) -> None:
        preview = self.draft.preview
        if preview is None or self.draft.profile_effective_hash is None:
            return
        if preview.profile_effective_hash != self.draft.profile_effective_hash:
            self.draft.preview = None
            self.phase = "Preview stale"
            self.error_message = "Profile behavior changed. Create a new preview."

    def _render_preview(self) -> None:
        status = self._status_text()
        preview_text = (
            plan_summary_text(self.draft.preview)
            if self.draft.preview is not None
            else "No current preview."
        )
        self.content_text = "\n\n".join([status, preview_text])
        if self.is_mounted:
            self.query_one("#workflow-preview-status", Static).update(status)
            self.query_one("#workflow-preview-summary", Static).update(preview_text)

    def _status_text(self) -> str:
        lines = [
            "New Workflow - Preview plan",
            "",
            f"Phase: {self.phase}",
            f"Selected files: {len(self.draft.selected_media_ids)}",
            f"Profile: {self.draft.profile_name or 'none'}",
        ]
        if self.error_message is not None:
            lines.extend(["", f"Error: {self.error_message}"])
        return "\n".join(lines)
