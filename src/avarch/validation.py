from __future__ import annotations

import asyncio
import math
import os
import subprocess
import tempfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlmodel import Session

from avarch.contracts import TRANSCODE_PLAN_SCHEMA_VERSION
from avarch.models.db import Job, JobAttempt, ValidationResult
from avarch.models.plan import ExecutionRuntimePaths, TranscodePlan
from avarch.models.scheduler import AttemptStatus, JobStage, JobStatus
from avarch.models.validation import (
    ObservedValidationMedia,
    ValidationCheck,
    ValidationCheckStatus,
    ValidationPolicy,
    ValidationReport,
    checks_pass,
)
from avarch.probe import (
    ProbeError,
    ProbeExecutableNotFoundError,
    build_ffprobe_command,
    normalize_probe,
    run_ffprobe,
)
from avarch.scanner import create_file_snapshot
from avarch.serialization import canonical_json

Clock = Callable[[], datetime]

DEFAULT_DECODE_TIMEOUT_SECONDS = 120.0
MAX_PROCESS_TAIL_BYTES = 16_384

CHECK_ORDER = [
    "source_fingerprint",
    "source_stable",
    "output_exists",
    "output_regular_file",
    "output_nonempty",
    "output_stable",
    "ffprobe_readable",
    "duration",
    "container",
    "video_stream_count",
    "video_codec",
    "resolution",
    "audio_stream_count",
    "audio_codec",
    "audio_channels",
    "audio_language",
    "subtitle_stream_count",
    "subtitle_policy",
    "minimum_output_size",
    "size_reduction",
    "decode_sample",
]


class ValidationError(RuntimeError):
    pass


class ValidationExecutionError(ValidationError):
    pass


class ValidationTargetError(ValidationError):
    pass


class ValidationPolicyError(ValidationError):
    pass


class ValidationPersistenceError(ValidationError):
    pass


async def validate_output(
    *,
    job: Job,
    plan: TranscodePlan,
    policy: ValidationPolicy,
    runtime_paths: ExecutionRuntimePaths,
    clock: Clock,
) -> ValidationReport:
    _verify_validation_contract(job=job, plan=plan, policy=policy)
    started_at = clock()
    checks: dict[str, ValidationCheck] = {}
    warnings: list[str] = []
    observed: ObservedValidationMedia | None = None

    source_before = _snapshot_fingerprint(plan.input_path)
    checks["source_fingerprint"] = _check(
        "source_fingerprint",
        source_before == plan.source_fs_fingerprint,
        expected=plan.source_fs_fingerprint,
        observed=source_before,
        message=(
            "The source filesystem fingerprint no longer matches the plan."
            if source_before != plan.source_fs_fingerprint
            else None
        ),
    )

    output_exists = plan.output_path.exists()
    checks["output_exists"] = _check(
        "output_exists",
        output_exists,
        expected=True,
        observed=output_exists,
    )
    output_regular = output_exists and plan.output_path.is_file()
    checks["output_regular_file"] = (
        _check("output_regular_file", output_regular, expected=True, observed=output_regular)
        if output_exists
        else _skipped("output_regular_file", "output_exists")
    )
    output_size = _output_size(plan.output_path) if output_regular else None
    output_nonempty = output_size is not None and output_size > 0
    checks["output_nonempty"] = (
        _check("output_nonempty", output_nonempty, expected=">0", observed=output_size)
        if output_regular
        else _skipped("output_nonempty", "output_regular_file")
    )

    output_before = _snapshot_fingerprint(plan.output_path) if output_nonempty else None
    ffprobe_command = build_ffprobe_command(plan.output_path)
    ffprobe_ok = False
    if output_nonempty:
        try:
            raw_probe = await asyncio.to_thread(run_ffprobe, plan.output_path)
            normalized = normalize_probe(raw_probe)
            observed = ObservedValidationMedia(
                output_size_bytes=output_size,
                container=normalized.container,
                duration_seconds=normalized.duration_seconds,
                video_streams=normalized.video_streams,
                audio_streams=normalized.audio_streams,
                subtitle_streams=normalized.subtitle_streams,
            )
            ffprobe_ok = True
            checks["ffprobe_readable"] = _passed(
                "ffprobe_readable",
                observed={"argv": ffprobe_command},
            )
        except ProbeExecutableNotFoundError as exc:
            raise ValidationExecutionError(str(exc)) from exc
        except ProbeError as exc:
            checks["ffprobe_readable"] = _failed(
                "ffprobe_readable",
                observed=exc.__class__.__name__,
                message=str(exc),
            )
    else:
        checks["ffprobe_readable"] = _skipped("ffprobe_readable", "output_nonempty")

    if ffprobe_ok and observed is not None:
        checks["duration"] = validate_duration(
            source_duration=policy.source_duration_seconds,
            output_duration=observed.duration_seconds,
            tolerance_seconds=policy.duration_tolerance_seconds,
        )
        checks["container"] = validate_container(
            observed_container=observed.container,
            accepted_container_names=policy.accepted_container_names,
        )
        for check in validate_video_streams(policy, observed):
            checks[check.name] = check
        for check in validate_audio_streams(policy, observed):
            checks[check.name] = check
        for check in validate_subtitle_streams(policy, observed):
            checks[check.name] = check
        checks["minimum_output_size"] = validate_output_size(
            output_size_bytes=output_size,
            source_size_bytes=policy.source_size_bytes,
            minimum_output_bytes=policy.minimum_output_bytes,
            minimum_output_source_ratio=policy.minimum_output_source_ratio,
        )
        checks["size_reduction"] = validate_size_reduction(
            output_size_bytes=output_size,
            source_size_bytes=policy.source_size_bytes,
            minimum_size_reduction_percent=policy.minimum_size_reduction_percent,
        )
    else:
        _skip_metadata_checks(checks, policy=policy, dependency="ffprobe_readable")

    checks["decode_sample"] = await _validate_decode_sample(
        plan=plan,
        policy=policy,
        observed=observed,
        runtime_paths=runtime_paths,
        ffprobe_ok=ffprobe_ok,
    )

    source_after = _snapshot_fingerprint(plan.input_path)
    output_after = _snapshot_fingerprint(plan.output_path) if output_before is not None else None
    checks["source_stable"] = _check(
        "source_stable",
        source_before is not None and source_before == source_after,
        expected=source_before,
        observed=source_after,
        message="Source changed during validation."
        if source_before is None or source_before != source_after
        else None,
    )
    checks["output_stable"] = (
        _check(
            "output_stable",
            output_before == output_after,
            expected=output_before,
            observed=output_after,
            message="Output changed during validation."
            if output_before != output_after
            else None,
        )
        if output_before is not None
        else _skipped("output_stable", "output_nonempty")
    )

    ordered_checks = [checks[name] for name in CHECK_ORDER]
    finished_at = clock()
    report = ValidationReport(
        plan_hash=plan.plan_hash,
        policy_hash=policy.policy_hash,
        source_path=plan.input_path,
        output_path=plan.output_path,
        source_fs_fingerprint_before=source_before,
        source_fs_fingerprint_after=source_after,
        output_fs_fingerprint_before=output_before,
        output_fs_fingerprint_after=output_after,
        passed=checks_pass(ordered_checks),
        checks=ordered_checks,
        warnings=warnings,
        observed=observed,
        started_at=started_at,
        finished_at=finished_at,
    )
    write_validation_report(runtime_paths.validation_report, report)
    return report


def validate_duration(
    *,
    source_duration: float,
    output_duration: float | None,
    tolerance_seconds: float,
) -> ValidationCheck:
    valid = (
        output_duration is not None
        and math.isfinite(source_duration)
        and math.isfinite(output_duration)
        and source_duration > 0
        and output_duration > 0
    )
    if not valid:
        return _failed(
            "duration",
            expected={"source": source_duration, "tolerance": tolerance_seconds},
            observed=output_duration,
            message="Duration must be finite and positive.",
        )
    assert output_duration is not None
    checked_output_duration = float(output_duration)
    delta = abs(checked_output_duration - source_duration)
    return _check(
        "duration",
        delta <= tolerance_seconds,
        expected={
            "source": source_duration,
            "tolerance": tolerance_seconds,
        },
        observed={
            "output": checked_output_duration,
            "absolute_delta": delta,
        },
    )


def validate_container(
    *,
    observed_container: str | None,
    accepted_container_names: list[str],
) -> ValidationCheck:
    normalized = _normalize_text(observed_container)
    accepted = [_normalize_text(value) for value in accepted_container_names]
    return _check(
        "container",
        normalized is not None and normalized in accepted,
        expected=accepted_container_names,
        observed=observed_container,
    )


def validate_video_streams(
    policy: ValidationPolicy,
    observed: ObservedValidationMedia,
) -> list[ValidationCheck]:
    checks = [
        _check(
            "video_stream_count",
            len(observed.video_streams) == policy.expected_video_stream_count,
            expected=policy.expected_video_stream_count,
            observed=len(observed.video_streams),
        )
    ]
    stream = observed.video_streams[0] if observed.video_streams else None
    checks.append(
        _check(
            "video_codec",
            stream is not None
            and _normalize_text(stream.codec) == _normalize_text(policy.expected_video_codec),
            expected=policy.expected_video_codec,
            observed=stream.codec if stream is not None else None,
        )
    )
    checks.append(
        _check(
            "resolution",
            stream is not None
            and stream.width == policy.expected_width
            and stream.height == policy.expected_height,
            expected={"width": policy.expected_width, "height": policy.expected_height},
            observed={
                "width": stream.width if stream is not None else None,
                "height": stream.height if stream is not None else None,
            },
        )
    )
    return checks


def validate_audio_streams(
    policy: ValidationPolicy,
    observed: ObservedValidationMedia,
) -> list[ValidationCheck]:
    checks = [
        _check(
            "audio_stream_count",
            len(observed.audio_streams) == policy.expected_audio_stream_count,
            expected=policy.expected_audio_stream_count,
            observed=len(observed.audio_streams),
        )
    ]
    stream = observed.audio_streams[0] if observed.audio_streams else None
    checks.append(
        _check(
            "audio_codec",
            stream is not None
            and _normalize_text(stream.codec) == _normalize_text(policy.expected_audio_codec),
            expected=policy.expected_audio_codec,
            observed=stream.codec if stream is not None else None,
        )
    )
    checks.append(
        _check(
            "audio_channels",
            stream is not None and stream.channels == policy.expected_audio_channels,
            expected=policy.expected_audio_channels,
            observed=stream.channels if stream is not None else None,
        )
    )
    if policy.expected_audio_language is None:
        checks.append(
            ValidationCheck(
                name="audio_language",
                status=ValidationCheckStatus.SKIPPED,
                required=False,
                message="No expected audio language is configured.",
            )
        )
    else:
        checks.append(
            _check(
                "audio_language",
                stream is not None
                and _normalize_text(stream.language)
                == _normalize_text(policy.expected_audio_language),
                expected=policy.expected_audio_language,
                observed=stream.language if stream is not None else None,
            )
        )
    return checks


def validate_subtitle_streams(
    policy: ValidationPolicy,
    observed: ObservedValidationMedia,
) -> list[ValidationCheck]:
    expected_count = len(policy.expected_subtitles)
    checks = [
        _check(
            "subtitle_stream_count",
            len(observed.subtitle_streams) == expected_count,
            expected=expected_count,
            observed=len(observed.subtitle_streams),
        )
    ]
    mismatches: list[dict[str, Any]] = []
    for expected in policy.expected_subtitles:
        stream = (
            observed.subtitle_streams[expected.output_order]
            if expected.output_order < len(observed.subtitle_streams)
            else None
        )
        if stream is None:
            mismatches.append({"output_order": expected.output_order, "reason": "missing"})
            continue
        if expected.codec is not None and _normalize_text(stream.codec) != _normalize_text(
            expected.codec
        ):
            mismatches.append(
                {
                    "output_order": expected.output_order,
                    "field": "codec",
                    "expected": expected.codec,
                    "observed": stream.codec,
                }
            )
        if expected.language is not None and _normalize_text(stream.language) != _normalize_text(
            expected.language
        ):
            mismatches.append(
                {
                    "output_order": expected.output_order,
                    "field": "language",
                    "expected": expected.language,
                    "observed": stream.language,
                }
            )
        if stream.forced != expected.forced:
            mismatches.append(
                {
                    "output_order": expected.output_order,
                    "field": "forced",
                    "expected": expected.forced,
                    "observed": stream.forced,
                }
            )
    checks.append(
        _check(
            "subtitle_policy",
            not mismatches,
            expected=[item.model_dump(mode="json") for item in policy.expected_subtitles],
            observed=[item.model_dump(mode="json") for item in observed.subtitle_streams],
            message=canonical_json(mismatches) if mismatches else None,
        )
    )
    return checks


def validate_output_size(
    *,
    output_size_bytes: int | None,
    source_size_bytes: int,
    minimum_output_bytes: int,
    minimum_output_source_ratio: float,
) -> ValidationCheck:
    ratio_threshold = math.ceil(source_size_bytes * minimum_output_source_ratio)
    required_minimum = max(minimum_output_bytes, ratio_threshold)
    return _check(
        "minimum_output_size",
        output_size_bytes is not None and output_size_bytes >= required_minimum,
        expected={
            "minimum_output_bytes": minimum_output_bytes,
            "minimum_output_source_ratio": minimum_output_source_ratio,
            "ratio_threshold": ratio_threshold,
            "required_minimum": required_minimum,
        },
        observed=output_size_bytes,
    )


def validate_size_reduction(
    *,
    output_size_bytes: int | None,
    source_size_bytes: int,
    minimum_size_reduction_percent: float | None,
) -> ValidationCheck:
    if minimum_size_reduction_percent is None:
        return ValidationCheck(
            name="size_reduction",
            status=ValidationCheckStatus.SKIPPED,
            required=False,
            message="Size reduction threshold is disabled.",
        )
    reduction = (
        ((source_size_bytes - output_size_bytes) / source_size_bytes) * 100
        if output_size_bytes is not None and source_size_bytes > 0
        else None
    )
    return _check(
        "size_reduction",
        reduction is not None and reduction >= minimum_size_reduction_percent,
        expected=minimum_size_reduction_percent,
        observed=reduction,
    )


def build_decode_sample_command(
    *,
    output_path: Path,
    sample_start: float,
    sample_duration: float,
    executable: str = "ffmpeg",
) -> list[str]:
    return [
        executable,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "error",
        "-xerror",
        "-ss",
        f"{sample_start:.6f}",
        "-i",
        str(output_path),
        "-map",
        "0:v:0",
        "-map",
        "0:a:0?",
        "-t",
        f"{sample_duration:.6f}",
        "-sn",
        "-dn",
        "-f",
        "null",
        "-",
    ]


def write_validation_report(path: Path, report: ValidationReport) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.tmp-",
        suffix=".json",
        text=True,
    )
    temporary_path = Path(raw_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output_file:
            output_file.write(canonical_json(report))
            output_file.write("\n")
            output_file.flush()
            os.fsync(output_file.fileno())
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return path


def persist_validation_result(
    session: Session,
    *,
    job: Job,
    attempt: JobAttempt,
    report: ValidationReport,
) -> ValidationResult:
    if job.id is None or attempt.id is None:
        raise ValidationPersistenceError("Job and attempt must be persisted.")
    now = report.finished_at
    result = ValidationResult(
        job_id=job.id,
        attempt_id=attempt.id,
        plan_hash=report.plan_hash,
        policy_hash=report.policy_hash,
        output_path=str(report.output_path),
        output_fs_fingerprint=report.output_fs_fingerprint_after,
        passed=report.passed,
        details_json=canonical_json(report),
        created_at=now,
    )
    session.add(result)
    session.flush()
    if result.id is None:
        raise ValidationPersistenceError("Validation result id was not assigned.")

    job.latest_validation_id = result.id
    job.claimed_by = None
    job.updated_at = now
    job.finished_at = None
    attempt.status = AttemptStatus.COMPLETED
    attempt.finished_at = now
    attempt.output_path = str(report.output_path)
    attempt.details_json = canonical_json(
        {
            "result_id": result.id,
            "report_path": str(report.output_path),
            "plan_hash": report.plan_hash,
            "policy_hash": report.policy_hash,
            "failed_checks": failed_required_check_names(report),
        }
    )
    if report.passed:
        job.status = JobStatus.VALIDATED
        job.stage = JobStage.PROMOTE
        job.last_error_type = None
        job.last_error_message = None
    else:
        job.status = JobStatus.FAILED
        job.stage = JobStage.VALIDATE
        job.finished_at = now
        job.last_error_type = "ValidationFailed"
        job.last_error_message = failed_check_summary(report)
    session.add(job)
    session.add(attempt)
    return result


def failed_required_check_names(report: ValidationReport) -> list[str]:
    return [
        check.name
        for check in report.checks
        if check.required and check.status != ValidationCheckStatus.PASS
    ]


def failed_check_summary(report: ValidationReport) -> str:
    names = failed_required_check_names(report)
    if not names:
        return "Validation failed."
    return "Validation failed required checks: " + ", ".join(names)


def format_validation_report_summary(
    report: ValidationReport,
    *,
    reused: bool = False,
) -> str:
    lines = ["Validation PASS" if report.passed else "Validation FAIL", ""]
    if reused:
        lines.append("Existing passing validation reused.")
        lines.append("")
    lines.extend(
        [
            f"Output:       {report.output_path}",
            f"Source:       {report.source_path}",
            f"Plan hash:    {report.plan_hash}",
            f"Policy hash:  {report.policy_hash}",
            "",
        ]
    )
    for check in report.checks:
        lines.append(f"{check.status.value.upper():<5} {check.name}")
    return "\n".join(lines)


async def _validate_decode_sample(
    *,
    plan: TranscodePlan,
    policy: ValidationPolicy,
    observed: ObservedValidationMedia | None,
    runtime_paths: ExecutionRuntimePaths,
    ffprobe_ok: bool,
) -> ValidationCheck:
    if not policy.decode_sample.enabled:
        return ValidationCheck(
            name="decode_sample",
            status=ValidationCheckStatus.SKIPPED,
            required=False,
            message="Decode sampling is disabled.",
        )
    if not ffprobe_ok or observed is None:
        return _skipped("decode_sample", "ffprobe_readable", required=True)
    duration = observed.duration_seconds
    if duration is None or not math.isfinite(duration) or duration <= 0:
        return ValidationCheck(
            name="decode_sample",
            status=ValidationCheckStatus.SKIPPED,
            required=True,
            message="Decode sampling requires a finite positive output duration.",
        )
    sample_duration = min(policy.decode_sample.duration_seconds, duration)
    sample_start = max(0.0, duration / 2.0 - sample_duration / 2.0)
    command = build_decode_sample_command(
        output_path=plan.output_path,
        sample_start=sample_start,
        sample_duration=sample_duration,
    )
    try:
        result = await asyncio.to_thread(
            _run_decode_sample,
            command,
            runtime_paths.validation_decode_stdout_log,
            runtime_paths.validation_decode_stderr_log,
        )
    except FileNotFoundError as exc:
        raise ValidationExecutionError("ffmpeg executable not found: ffmpeg") from exc
    except subprocess.TimeoutExpired:
        return _failed(
            "decode_sample",
            expected={"argv": command, "timeout_seconds": DEFAULT_DECODE_TIMEOUT_SECONDS},
            observed="timeout",
            message=f"Decode sample timed out after {DEFAULT_DECODE_TIMEOUT_SECONDS:g} seconds.",
        )
    if result.returncode == 0:
        return _passed(
            "decode_sample",
            expected={"argv": command},
            observed={"returncode": result.returncode},
        )
    return _failed(
        "decode_sample",
        expected={"argv": command},
        observed={
            "returncode": result.returncode,
            "stderr_tail": _tail(result.stderr),
        },
        message=f"Decode sample failed with exit code {result.returncode}.",
    )


def _run_decode_sample(
    command: list[str],
    stdout_log: Path,
    stderr_log: Path,
) -> subprocess.CompletedProcess[str]:
    stdout_log.parent.mkdir(parents=True, exist_ok=True)
    stderr_log.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        timeout=DEFAULT_DECODE_TIMEOUT_SECONDS,
    )
    stdout_log.write_text(result.stdout, encoding="utf-8")
    stderr_log.write_text(result.stderr, encoding="utf-8")
    return result


def _verify_validation_contract(
    *,
    job: Job,
    plan: TranscodePlan,
    policy: ValidationPolicy,
) -> None:
    if plan.schema_version != TRANSCODE_PLAN_SCHEMA_VERSION:
        raise ValidationPolicyError(
            "Unsupported plan schema.\n\nRegenerate this plan with the current avarch version."
        )
    if policy.schema_version != 2:
        raise ValidationPolicyError("Unsupported validation policy schema.")
    if job.plan_hash != plan.plan_hash:
        raise ValidationTargetError("Job plan hash does not match the validation plan.")
    if job.output_path is not None and Path(job.output_path) != plan.output_path:
        raise ValidationTargetError("Job output path does not match the validation plan.")
    if job.media_file_id != plan.media_file_id:
        raise ValidationTargetError("Job media file does not match the validation plan.")


def _skip_metadata_checks(
    checks: dict[str, ValidationCheck],
    *,
    policy: ValidationPolicy,
    dependency: str,
) -> None:
    for name in (
        "duration",
        "container",
        "video_stream_count",
        "video_codec",
        "resolution",
        "audio_stream_count",
        "audio_codec",
        "audio_channels",
        "subtitle_stream_count",
        "subtitle_policy",
        "minimum_output_size",
    ):
        checks[name] = _skipped(name, dependency)
    checks["audio_language"] = _skipped(
        "audio_language",
        dependency,
        required=policy.expected_audio_language is not None,
    )
    checks["size_reduction"] = _skipped(
        "size_reduction",
        dependency,
        required=policy.minimum_size_reduction_percent is not None,
    )


def _snapshot_fingerprint(path: Path) -> str | None:
    try:
        return create_file_snapshot(path).fs_fingerprint
    except OSError:
        return None


def _output_size(path: Path) -> int | None:
    try:
        return path.stat(follow_symlinks=False).st_size
    except OSError:
        return None


def _check(
    name: str,
    passed: bool,
    *,
    expected: Any = None,
    observed: Any = None,
    required: bool = True,
    message: str | None = None,
) -> ValidationCheck:
    return ValidationCheck(
        name=name,
        status=ValidationCheckStatus.PASS if passed else ValidationCheckStatus.FAIL,
        required=required,
        expected=expected,
        observed=observed,
        message=message,
    )


def _passed(name: str, *, expected: Any = None, observed: Any = None) -> ValidationCheck:
    return ValidationCheck(
        name=name,
        status=ValidationCheckStatus.PASS,
        required=True,
        expected=expected,
        observed=observed,
    )


def _failed(
    name: str,
    *,
    expected: Any = None,
    observed: Any = None,
    message: str | None = None,
) -> ValidationCheck:
    return ValidationCheck(
        name=name,
        status=ValidationCheckStatus.FAIL,
        required=True,
        expected=expected,
        observed=observed,
        message=message,
    )


def _skipped(name: str, dependency: str, *, required: bool = True) -> ValidationCheck:
    return ValidationCheck(
        name=name,
        status=ValidationCheckStatus.SKIPPED,
        required=required,
        message=f"Skipped because {dependency} did not pass.",
    )


def _normalize_text(value: str | None) -> str | None:
    if value is None:
        return None
    return value.strip().lower()


def _tail(value: str) -> str:
    encoded = value.encode("utf-8", errors="replace")
    tail = encoded[-MAX_PROCESS_TAIL_BYTES:]
    if len(encoded) > MAX_PROCESS_TAIL_BYTES:
        tail = b"... truncated ...\n" + tail
    return tail.decode("utf-8", errors="replace")
