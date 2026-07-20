from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from avarch.application.attempt_metrics import AttemptMetricsAccumulator, AttemptMetricsSummary
from avarch.application.benchmark_samples import BenchmarkSample
from avarch.application.candidate_tournament import CandidateKey, candidate_key
from avarch.application.performance_candidates import PerformanceCandidate
from avarch.domain.encoder_args import LP_OPTIONS, normalize_svt_operational_args
from avarch.domain.jobs import AttemptStatus
from avarch.domain.progress import ProgressSnapshot
from avarch.models.execution import (
    ProcessCancellationToken,
    ProcessFailureReason,
    ProcessResult,
)
from avarch.models.plan import Av1anCommandSpec, TranscodePlan


class CalibrationMeasurementStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RESOURCE_PRESSURE = "resource_pressure"
    INVALID_OUTPUT = "invalid_output"


@dataclass(frozen=True, slots=True)
class CalibrationMeasurementResult:
    candidate_key: CandidateKey
    status: CalibrationMeasurementStatus
    command: tuple[str, ...]
    metrics: AttemptMetricsSummary
    output_path: Path
    temp_dir: Path
    elapsed_seconds: float
    failure_reason: ProcessFailureReason | None = None


class CalibrationProcessRunner(Protocol):
    def run(
        self,
        spec: Av1anCommandSpec,
        *,
        sample: BenchmarkSample,
        cancellation_token: ProcessCancellationToken | None,
        progress_sink: CalibrationProgressSink,
        timeout_seconds: float | None,
    ) -> ProcessResult: ...


class CalibrationProgressSink(Protocol):
    def publish(self, snapshot: ProgressSnapshot) -> None: ...


class Av1anCommandRenderer(Protocol):
    def __call__(self, spec: Av1anCommandSpec, *, resume: bool) -> list[str]: ...


class _MetricsProgressSink:
    def __init__(self, accumulator: AttemptMetricsAccumulator) -> None:
        self._accumulator = accumulator

    def publish(self, snapshot: ProgressSnapshot) -> None:
        self._accumulator.record_progress(snapshot)


def measure_candidate(
    *,
    plan: TranscodePlan,
    candidate: PerformanceCandidate,
    sample: BenchmarkSample,
    calibration_dir: Path,
    runner: CalibrationProcessRunner,
    command_renderer: Av1anCommandRenderer,
    cancellation_token: ProcessCancellationToken | None = None,
    warmup_seconds: float = 5.0,
    timeout_seconds: float | None = None,
) -> CalibrationMeasurementResult:
    spec = calibration_av1an_spec(
        plan=plan,
        candidate=candidate,
        sample=sample,
        calibration_dir=calibration_dir,
    )
    command = tuple(command_renderer(spec, resume=False))
    accumulator = AttemptMetricsAccumulator(warmup=timedelta(seconds=warmup_seconds))
    progress_sink = _MetricsProgressSink(accumulator)
    result = runner.run(
        spec,
        sample=sample,
        cancellation_token=cancellation_token,
        progress_sink=progress_sink,
        timeout_seconds=timeout_seconds,
    )
    if result.resource_summary is not None:
        accumulator.record_resource_summary(result.resource_summary)
    status = _measurement_status(result=result, output_path=spec.video_output_path)
    metrics_status = (
        AttemptStatus.COMPLETED
        if status is CalibrationMeasurementStatus.COMPLETED
        else AttemptStatus.FAILED
    )
    return CalibrationMeasurementResult(
        candidate_key=candidate_key(candidate),
        status=status,
        command=command,
        metrics=accumulator.finalize(status=metrics_status),
        output_path=spec.video_output_path,
        temp_dir=spec.temp_dir,
        elapsed_seconds=(result.finished_at - result.started_at).total_seconds(),
        failure_reason=result.failure_reason,
    )


def calibration_av1an_spec(
    *,
    plan: TranscodePlan,
    candidate: PerformanceCandidate,
    sample: BenchmarkSample,
    calibration_dir: Path,
) -> Av1anCommandSpec:
    key = _candidate_path_key(candidate_key(candidate))
    candidate_dir = calibration_dir / key
    if candidate_dir.exists():
        shutil.rmtree(candidate_dir)
    video_output_path = candidate_dir / "candidate.mkv"
    temp_dir = candidate_dir / "av1an"
    sample_script = candidate_dir / "sample.vpy"
    _write_sample_script(
        source_script=plan.vapoursynth.script_path,
        output_script=sample_script,
        sample=sample,
    )
    return plan.av1an.model_copy(
        update={
            "input_path": sample_script,
            "video_output_path": video_output_path,
            "temp_dir": temp_dir,
            "working_directory": candidate_dir,
            "workers": candidate.workers,
            "encoder_args": _candidate_encoder_args(plan.av1an.encoder_args, candidate),
            "resume_policy": "never",
        }
    )


def _write_sample_script(
    *,
    source_script: Path,
    output_script: Path,
    sample: BenchmarkSample,
) -> None:
    frame_rate = sample.estimated_frames / sample.duration_seconds
    start_frame = max(0, round(sample.start_seconds * frame_rate))
    end_frame = start_frame + sample.estimated_frames
    output_script.parent.mkdir(parents=True, exist_ok=True)
    output_script.write_text(
        "# Generated by avarch for isolated calibration.\n"
        "from pathlib import Path as _AvarchPath\n"
        "import vapoursynth as _avarch_vs\n\n"
        f"_avarch_source = _AvarchPath({str(source_script)!r})\n"
        "globals()['__file__'] = str(_avarch_source)\n"
        "exec(compile(_avarch_source.read_bytes(), str(_avarch_source), 'exec'), globals())\n"
        "_avarch_output = _avarch_vs.get_output(0)\n"
        "_avarch_clip = _avarch_output.clip\n"
        f"_avarch_start = min({start_frame}, max(0, _avarch_clip.num_frames - 1))\n"
        f"_avarch_end = min({end_frame}, _avarch_clip.num_frames)\n"
        "if _avarch_end <= _avarch_start:\n"
        "    raise RuntimeError('calibration sample is outside the source clip')\n"
        "_avarch_vs.clear_output(0)\n"
        "_avarch_clip[_avarch_start:_avarch_end].set_output(index=0)\n",
        encoding="utf-8",
    )


def _measurement_status(
    *,
    result: ProcessResult,
    output_path: Path,
) -> CalibrationMeasurementStatus:
    if result.failure_reason is ProcessFailureReason.CANCELLED:
        return CalibrationMeasurementStatus.CANCELLED
    if result.failure_reason in {
        ProcessFailureReason.RESOURCE_OOM,
        ProcessFailureReason.RESOURCE_PRESSURE,
    }:
        return CalibrationMeasurementStatus.RESOURCE_PRESSURE
    if not result.succeeded:
        return CalibrationMeasurementStatus.FAILED
    if not output_path.exists() or output_path.stat().st_size <= 0:
        return CalibrationMeasurementStatus.INVALID_OUTPUT
    return CalibrationMeasurementStatus.COMPLETED


def _candidate_encoder_args(
    arguments: list[str],
    candidate: PerformanceCandidate,
) -> list[str]:
    stripped = _strip_svt_lp(arguments)
    structured_lp = candidate.svt_lp if isinstance(candidate.svt_lp, int) else "native"
    return normalize_svt_operational_args(stripped, structured_svt_lp=structured_lp)


def _strip_svt_lp(arguments: list[str]) -> list[str]:
    stripped: list[str] = []
    index = 0
    while index < len(arguments):
        token = arguments[index]
        option, separator, _inline_value = token.partition("=")
        if option not in LP_OPTIONS:
            stripped.append(token)
            index += 1
            continue
        index += 1 if separator else 2
    return stripped


def _candidate_path_key(key: CandidateKey) -> str:
    workers = "auto" if key.workers == "auto" else str(key.workers)
    lp = "native" if key.svt_lp == "native" else str(key.svt_lp)
    return f"workers-{workers}_lp-{lp}"
