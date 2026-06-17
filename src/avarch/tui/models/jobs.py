from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from avarch.tui.models.common import UiRevision


@dataclass(frozen=True, slots=True)
class JobAttemptSnapshot:
    attempt_id: int
    attempt_number: int
    stage: str
    resource_class: str
    status: str
    runner_id: str
    started_at: datetime
    finished_at: datetime | None
    stdout_log: str | None
    stderr_log: str | None
    output_path: str | None
    error_type: str | None
    error_message: str | None


@dataclass(frozen=True, slots=True)
class JobEventSnapshot:
    event_id: int
    event_type: str
    actor: str
    reason: str | None
    details_json: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class ValidationSnapshot:
    validation_id: int
    passed: bool
    output_path: str
    plan_hash: str
    policy_hash: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class PromotionSnapshot:
    promotion_id: int
    mode: str
    status: str
    phase: str
    source_path: str
    final_path: str
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class JobPlanSnapshot:
    plan_hash: str
    plan_path: str
    artifact_text: str
    artifact_read_only: bool
    video_stream_index: int
    audio_stream_index: int
    subtitle_stream_indexes: tuple[int, ...]
    vapoursynth_mode: str
    vapoursynth_identity_hash: str
    vapoursynth_template_path: str | None
    vapoursynth_template_hash: str | None
    validation_policy_hash: str
    validation_expected_video_codec: str
    validation_expected_width: int
    validation_expected_height: int
    validation_expected_audio_codec: str
    validation_expected_audio_channels: int
    validation_expected_audio_language: str | None
    validation_decode_sample: bool


@dataclass(frozen=True, slots=True)
class JobDetailSnapshot:
    revision: UiRevision
    job_id: int
    media_file_id: int
    file_name: str
    source_path: str
    status: str
    stage: str
    profile_name: str
    profile_hash: str
    priority: int
    attempts_count: int
    queue_key: str
    source_fs_fingerprint: str
    probe_hash: str | None
    plan_hash: str | None
    plan_path: str | None
    output_path: str | None
    last_error_type: str | None
    last_error_message: str | None
    control_request: str | None
    control_reason: str | None
    plan: JobPlanSnapshot | None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    attempts: tuple[JobAttemptSnapshot, ...]
    events: tuple[JobEventSnapshot, ...]
    latest_validation: ValidationSnapshot | None
    latest_promotion: PromotionSnapshot | None


@dataclass(frozen=True, slots=True)
class JobLogSnapshot:
    job_id: int
    attempt_number: int | None
    tail_bytes: int
    stdout_tail: str | None
    stderr_tail: str | None
    truncated: bool
