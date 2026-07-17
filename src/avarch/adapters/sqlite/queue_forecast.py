from __future__ import annotations

import json
import re
import shlex
from datetime import timedelta
from typing import Any

from sqlmodel import Session, col, select

from avarch.adapters.sqlite.models import Job, JobAttempt, MediaPlan, ProbeResult
from avarch.application.probe_summary import ProbeSummaryError, parse_normalized_probe_json
from avarch.application.queue_forecast import ComparableEncodeKey, ComparableEncodeSample
from avarch.domain.jobs import AttemptStatus, JobStage

_MAX_REASONABLE_DURATION = timedelta(days=30)


def comparable_encode_history(
    session: Session,
    *,
    limit: int = 200,
) -> tuple[ComparableEncodeSample, ...]:
    rows = session.exec(
        select(JobAttempt, Job, MediaPlan, ProbeResult)
        .join(Job, Job.id == JobAttempt.job_id)
        .join(MediaPlan, MediaPlan.plan_hash == Job.plan_hash)
        .join(ProbeResult, ProbeResult.id == MediaPlan.probe_result_id)
        .where(
            JobAttempt.status == AttemptStatus.COMPLETED,
            JobAttempt.stage == JobStage.ENCODE,
        )
        .order_by(col(JobAttempt.finished_at).desc(), col(JobAttempt.id).desc())
        .limit(limit)
    ).all()
    samples: list[ComparableEncodeSample] = []
    for attempt, job, plan, probe in rows:
        sample = _history_sample(attempt=attempt, job=job, plan=plan, probe=probe)
        if sample is not None:
            samples.append(sample)
    return tuple(samples)


def _history_sample(
    *,
    attempt: JobAttempt,
    job: Job,
    plan: MediaPlan,
    probe: ProbeResult,
) -> ComparableEncodeSample | None:
    if attempt.id is None or job.id is None:
        return None
    if attempt.started_at is None or attempt.finished_at is None:
        return None
    duration = attempt.finished_at - attempt.started_at
    if duration <= timedelta(0) or duration > _MAX_REASONABLE_DURATION:
        return None
    try:
        normalized = parse_normalized_probe_json(probe.normalized_json)
    except ProbeSummaryError:
        return None
    video = min(normalized.video_streams, key=lambda stream: stream.index, default=None)
    if video is None or video.width is None or video.height is None:
        return None
    command = _command_metadata(attempt.command_json)
    key = ComparableEncodeKey(
        profile_name=job.profile_name,
        resolution_class=_resolution_class(width=video.width, height=video.height),
        bit_depth=video.bit_depth,
        encoder=command.get("encoder"),
        preset=command.get("preset"),
        vapoursynth_identity=plan.execution_identity_hash,
        workers=_int_or_none(command.get("workers")),
    )
    return ComparableEncodeSample(
        job_id=job.id,
        attempt_id=attempt.id,
        key=key,
        duration_seconds=int(duration.total_seconds()),
        finished_at=attempt.finished_at,
    )


def _command_metadata(command_json: str | None) -> dict[str, str]:
    if command_json is None:
        return {}
    try:
        value = json.loads(command_json)
    except json.JSONDecodeError:
        return {}
    if not isinstance(value, dict):
        return {}
    argv = value.get("av1an_argv")
    if not isinstance(argv, list):
        return {}
    args = [str(arg) for arg in argv]
    return {
        "encoder": _option_value(args, "--encoder"),
        "workers": _option_value(args, "--workers"),
        "preset": _preset_from_video_params(_option_value(args, "--video-params")),
    }


def _option_value(args: list[str], name: str) -> str | None:
    try:
        index = args.index(name)
    except ValueError:
        return None
    next_index = index + 1
    if next_index >= len(args):
        return None
    return args[next_index]


def _preset_from_video_params(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        args = shlex.split(value)
    except ValueError:
        args = value.split()
    for flag in ("--preset", "-preset"):
        preset = _option_value(args, flag)
        if preset is not None:
            return preset
    match = re.search(r"(?:^|\s)preset[ =:]+(\S+)", value)
    return match.group(1) if match else None


def _resolution_class(*, width: int, height: int) -> str:
    long_edge = max(width, height)
    if long_edge >= 3840:
        return "2160p"
    if long_edge >= 2560:
        return "1440p"
    if long_edge >= 1920:
        return "1080p"
    if long_edge >= 1280:
        return "720p"
    return "sd"


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
