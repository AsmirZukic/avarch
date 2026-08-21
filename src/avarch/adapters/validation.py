from __future__ import annotations

import asyncio
import math
import subprocess
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from avarch.adapters.filesystem.scanner import create_file_snapshot
from avarch.adapters.filesystem.validation_reports import write_validation_report
from avarch.adapters.probe import (
    ProbeError,
    ProbeExecutableNotFoundError,
    build_ffprobe_command,
    normalize_probe,
    run_ffprobe,
)
from avarch.domain.validation import ValidationCheckStatus, checks_pass
from avarch.models.plan import ExecutionRuntimePaths, TranscodePlan
from avarch.models.validation import (
    ObservedValidationMedia,
    ValidationCheck,
    ValidationPolicy,
    ValidationReport,
)
from avarch.serialization import canonical_json

Clock = Callable[[], datetime]


class ValidationJob(Protocol):
    @property
    def plan_hash(self) -> str | None: ...

    @property
    def output_path(self) -> str | None: ...

    @property
    def media_file_id(self) -> int | None: ...


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


async def validate_output(
    *,
    job: ValidationJob,
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
            message="Output changed during validation." if output_before != output_after else None,
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
    if policy.expected_audio_stream_count == 0:
        checks.append(
            ValidationCheck(
                name="audio_codec",
                status=ValidationCheckStatus.SKIPPED,
                required=False,
                message="No output audio stream is expected.",
            )
        )
        checks.append(
            ValidationCheck(
                name="audio_channels",
                status=ValidationCheckStatus.SKIPPED,
                required=False,
                message="No output audio stream is expected.",
            )
        )
        checks.append(
            ValidationCheck(
                name="audio_language",
                status=ValidationCheckStatus.SKIPPED,
                required=False,
                message="No output audio stream is expected.",
            )
        )
        return checks

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
    job: ValidationJob,
    plan: TranscodePlan,
    policy: ValidationPolicy,
) -> None:
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
