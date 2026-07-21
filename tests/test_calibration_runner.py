from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from avarch.adapters.execution import build_av1an_command
from avarch.application.benchmark_samples import BenchmarkSample
from avarch.application.calibration_orchestrator import run_calibration
from avarch.application.calibration_runner import (
    CalibrationMeasurementStatus,
    CalibrationProgressSink,
    calibration_av1an_spec,
    measure_candidate,
)
from avarch.application.candidate_tournament import CandidateKey
from avarch.application.performance_candidates import (
    PerformanceCandidate,
    generate_safe_concurrency_candidates,
)
from avarch.config import PerformanceSettings
from avarch.domain.progress import (
    ProgressPhase,
    ProgressSnapshot,
    ProgressSource,
    ProgressUnit,
)
from avarch.domain.resource_policy import parse_resource_intent
from avarch.domain.scheduler import ResourceCapacity
from avarch.models.execution import (
    ProcessCancellationToken,
    ProcessFailureReason,
    ProcessResourceSummary,
    ProcessResult,
    ProcessTerminationReason,
)
from avarch.models.plan import Av1anCommandSpec
from tests.test_plan_models import sample_plan

NOW = datetime(2026, 7, 1, 12, tzinfo=UTC)


def test_calibration_spec_overrides_only_operational_paths_and_candidate_values(
    tmp_path: Path,
) -> None:
    plan = sample_plan()
    candidate = _manual_candidate()

    spec = calibration_av1an_spec(
        plan=plan,
        candidate=candidate,
        sample=_sample(),
        calibration_dir=tmp_path / "calibration",
    )

    assert spec.input_path == tmp_path / "calibration" / "workers-2_lp-4" / "sample.vpy"
    sample_script = spec.input_path.read_text(encoding="utf-8")
    assert str(plan.vapoursynth.script_path) in sample_script
    assert "_avarch_start" in sample_script
    assert ".set_output(index=0)" in sample_script
    assert spec.video_output_path == tmp_path / "calibration" / "workers-2_lp-4" / "candidate.mkv"
    assert spec.temp_dir == tmp_path / "calibration" / "workers-2_lp-4" / "av1an"
    assert spec.working_directory == tmp_path / "calibration" / "workers-2_lp-4"
    assert spec.workers == 2
    assert spec.encoder_args[-2:] == ["--lp", "4"]
    assert spec.resume_policy == "never"
    assert spec.video_output_path != plan.av1an.video_output_path


def test_measurement_records_command_and_excludes_warmup_progress(tmp_path: Path) -> None:
    runner = _FakeRunner(write_output=True)
    result = measure_candidate(
        plan=sample_plan(),
        candidate=_manual_candidate(),
        sample=_sample(),
        calibration_dir=tmp_path / "calibration",
        runner=runner,
        command_renderer=build_av1an_command,
        warmup_seconds=5,
    )

    assert result.status == CalibrationMeasurementStatus.COMPLETED
    assert result.output_path == tmp_path / "calibration" / "workers-2_lp-4" / "candidate.mkv"
    assert result.output_path.exists()
    assert "--workers" in result.command
    assert result.command[result.command.index("--workers") + 1] == "2"
    assert "--lp 4" in result.command[result.command.index("--video-params") + 1]
    assert result.metrics.aggregate_fps == 10.0
    assert result.metrics.warmup_seconds == 5.0


def test_measurement_classifies_resource_pressure(tmp_path: Path) -> None:
    runner = _FakeRunner(
        result=ProcessResult.exited(
            command=("av1an",),
            return_code=137,
            started_at=NOW,
            finished_at=NOW + timedelta(seconds=5),
            resource_summary=ProcessResourceSummary(memory_oom_kill_events_delta=1),
        )
    )

    result = measure_candidate(
        plan=sample_plan(),
        candidate=_manual_candidate(),
        sample=_sample(),
        calibration_dir=tmp_path / "calibration",
        runner=runner,
        command_renderer=build_av1an_command,
    )

    assert result.status == CalibrationMeasurementStatus.RESOURCE_PRESSURE
    assert result.failure_reason == ProcessFailureReason.RESOURCE_OOM
    assert result.metrics.incomplete is True


def test_measurement_classifies_cancelled_and_invalid_output(tmp_path: Path) -> None:
    cancelled = measure_candidate(
        plan=sample_plan(),
        candidate=_manual_candidate(),
        sample=_sample(),
        calibration_dir=tmp_path / "cancelled",
        runner=_FakeRunner(
            result=ProcessResult.cancelled(
                command=("av1an",),
                return_code=-15,
                started_at=NOW,
                finished_at=NOW + timedelta(seconds=5),
                termination_requested_at=NOW + timedelta(seconds=4),
            )
        ),
        command_renderer=build_av1an_command,
    )
    invalid = measure_candidate(
        plan=sample_plan(),
        candidate=_manual_candidate(),
        sample=_sample(),
        calibration_dir=tmp_path / "invalid",
        runner=_FakeRunner(write_output=False),
        command_renderer=build_av1an_command,
    )

    assert cancelled.status == CalibrationMeasurementStatus.CANCELLED
    assert invalid.status == CalibrationMeasurementStatus.INVALID_OUTPUT


def test_orchestrator_attempts_every_candidate_with_shared_absolute_deadline(
    tmp_path: Path,
) -> None:
    runner = _FakeRunner(write_output=True)
    messages: list[str] = []

    def status_sink(message: str) -> None:
        messages.append(message)

    result = run_calibration(
        plan=sample_plan(),
        settings=PerformanceSettings(
            calibration_max_seconds=120,
            calibration_max_predicted_fraction=0.01,
            calibration_max_candidates=5,
            calibration_warmup_seconds=0,
        ),
        intent=parse_resource_intent(workers="auto"),
        capacity=ResourceCapacity(
            cheap_workers=1,
            av1an_jobs=1,
            file_ops=1,
            cpu_budget=19,
            memory_budget_bytes=16 * 1024**3,
        ),
        observations=(),
        frame_rate=24.0,
        predicted_encode_seconds=1_319.0,
        calibration_dir=tmp_path / "calibration",
        runner=runner,
        command_renderer=build_av1an_command,
        cancellation_token=ProcessCancellationToken(),
        status_sink=status_sink,
        monotonic=_SequenceClock((0.0, 0.0, 30.0, 40.0, 50.0, 60.0, 100.0)),
    )

    assert result.status == "completed"
    assert result.budget_seconds == 120
    assert len(result.candidates) == 5
    assert len(result.measurements) == 5
    assert len(runner.timeouts) == 5
    assert runner.timeouts == [120.0, 90.0, 80.0, 70.0, 60.0]
    assert messages[-1] == "calibration candidate 5/5 completed"


def test_orchestrator_uses_partial_measurements_when_budget_expires(
    tmp_path: Path,
) -> None:
    runner = _FakeRunner(write_output=True)

    result = run_calibration(
        plan=sample_plan(),
        settings=PerformanceSettings(
            calibration_max_seconds=30,
            calibration_max_predicted_fraction=0.01,
            calibration_max_candidates=5,
            calibration_warmup_seconds=0,
        ),
        intent=parse_resource_intent(workers="auto"),
        capacity=ResourceCapacity(
            cheap_workers=1,
            av1an_jobs=1,
            file_ops=1,
            cpu_budget=19,
            memory_budget_bytes=16 * 1024**3,
        ),
        observations=(),
        frame_rate=24.0,
        predicted_encode_seconds=1_319.0,
        calibration_dir=tmp_path / "partial",
        runner=runner,
        command_renderer=build_av1an_command,
        cancellation_token=ProcessCancellationToken(),
        monotonic=_SequenceClock((0.0, 0.0, 10.0, 30.0)),
    )

    assert result.status == "completed"
    assert result.reason == "partial_native_baseline"
    assert result.selection is not None
    assert result.selection.reason == "partial_calibration_measurement"
    assert result.selection.evidence_count == 2
    assert result.selection.confidence == 0.5
    assert len(result.candidates) == 5
    assert len(result.measurements) == 2


def test_orchestrator_scores_post_warmup_fps_and_remeasures_close_finalists(
    tmp_path: Path,
) -> None:
    clock = _MutableClock()
    runner = _AdaptiveRunner(
        clock=clock,
        fps_by_candidate={
            ("auto", "native"): 62.0,
            (4, 4): 60.0,
            (5, 3): 48.0,
            (3, 6): 45.0,
            (6, 3): 40.0,
        },
        first_wall_seconds_by_candidate={("auto", "native"): 30.0},
    )

    result = run_calibration(
        plan=sample_plan(),
        settings=PerformanceSettings(
            calibration_max_seconds=120,
            calibration_max_candidates=5,
            calibration_warmup_seconds=0,
        ),
        intent=parse_resource_intent(workers="auto"),
        capacity=ResourceCapacity(
            cheap_workers=1,
            av1an_jobs=1,
            file_ops=1,
            cpu_budget=19,
            memory_budget_bytes=16 * 1024**3,
        ),
        observations=(),
        frame_rate=24.0,
        predicted_encode_seconds=1_319.0,
        calibration_dir=tmp_path / "adaptive",
        runner=runner,
        command_renderer=build_av1an_command,
        cancellation_token=ProcessCancellationToken(),
        monotonic=clock,
    )

    assert result.status == "completed"
    assert result.selection is not None
    assert result.selection.effective_workers == "auto"
    assert result.selection.effective_svt_lp == "native"
    assert result.selection.confidence == 0.8
    measured_keys = [measurement.candidate_key for measurement in result.measurements]
    assert measured_keys.count(CandidateKey(workers="auto", svt_lp="native")) == 4
    assert measured_keys.count(CandidateKey(workers=4, svt_lp=4)) == 4
    assert len(result.measurements) == 11


def _manual_candidate() -> PerformanceCandidate:
    return generate_safe_concurrency_candidates(
        intent=parse_resource_intent(mode="manual", workers=2, svt_lp=4),
        capacity=ResourceCapacity(
            cheap_workers=1,
            av1an_jobs=1,
            file_ops=1,
            cpu_budget=16,
            memory_budget_bytes=16 * 1024**3,
        ),
        observations=(_Observation(peak_memory_bytes=1 * 1024**3),),
    )[0]


def _sample() -> BenchmarkSample:
    return BenchmarkSample(
        start_seconds=120.0,
        duration_seconds=30.0,
        estimated_frames=720,
        parallel_units=8,
    )


@dataclass(frozen=True, slots=True)
class _Observation:
    peak_memory_bytes: int | None


class _SequenceClock:
    def __init__(self, values: tuple[float, ...]) -> None:
        self._values = iter(values)

    def __call__(self) -> float:
        return next(self._values)


class _MutableClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _AdaptiveRunner:
    def __init__(
        self,
        *,
        clock: _MutableClock,
        fps_by_candidate: dict[tuple[int | str, int | str], float],
        first_wall_seconds_by_candidate: dict[tuple[int | str, int | str], float],
    ) -> None:
        self._clock = clock
        self._fps_by_candidate = fps_by_candidate
        self._first_wall_seconds_by_candidate = first_wall_seconds_by_candidate
        self._counts: dict[tuple[int | str, int | str], int] = {}

    def run(
        self,
        spec: Av1anCommandSpec,
        *,
        sample: BenchmarkSample,
        cancellation_token: ProcessCancellationToken | None,
        progress_sink: CalibrationProgressSink,
        timeout_seconds: float | None,
    ) -> ProcessResult:
        del sample, cancellation_token, timeout_seconds
        key = _spec_key(spec)
        count = self._counts.get(key, 0)
        self._counts[key] = count + 1
        wall_seconds = (
            self._first_wall_seconds_by_candidate.get(key, 5.0) if count == 0 else 5.0
        )
        fps = self._fps_by_candidate[key]
        progress_sink.publish(_progress(0, seconds=0))
        progress_sink.publish(_progress(round(fps * 5), seconds=5))
        spec.video_output_path.parent.mkdir(parents=True, exist_ok=True)
        spec.video_output_path.write_bytes(b"candidate")
        self._clock.advance(wall_seconds)
        return ProcessResult(
            command=("av1an",),
            return_code=0,
            started_at=NOW,
            finished_at=NOW + timedelta(seconds=wall_seconds),
            termination_reason=ProcessTerminationReason.EXITED,
        )


def _spec_key(spec: Av1anCommandSpec) -> tuple[int | str, int | str]:
    try:
        lp_index = spec.encoder_args.index("--lp")
    except ValueError:
        lp: int | str = "native"
    else:
        lp = int(spec.encoder_args[lp_index + 1])
    return spec.workers, lp


class _FakeRunner:
    def __init__(
        self,
        *,
        write_output: bool = False,
        result: ProcessResult | None = None,
    ) -> None:
        self._write_output = write_output
        self._result = result
        self.spec: Av1anCommandSpec | None = None
        self.timeouts: list[float | None] = []

    def run(
        self,
        spec: Av1anCommandSpec,
        *,
        sample: BenchmarkSample,
        cancellation_token: ProcessCancellationToken | None,
        progress_sink: CalibrationProgressSink,
        timeout_seconds: float | None,
    ) -> ProcessResult:
        del sample, cancellation_token
        self.spec = spec
        self.timeouts.append(timeout_seconds)
        progress_sink.publish(_progress(0, seconds=0))
        progress_sink.publish(_progress(50, seconds=5))
        progress_sink.publish(_progress(100, seconds=10))
        if self._write_output:
            spec.video_output_path.parent.mkdir(parents=True, exist_ok=True)
            spec.video_output_path.write_bytes(b"candidate")
        if self._result is not None:
            return self._result
        return ProcessResult(
            command=("av1an",),
            return_code=0,
            started_at=NOW,
            finished_at=NOW + timedelta(seconds=10),
            termination_reason=ProcessTerminationReason.EXITED,
            resource_summary=ProcessResourceSummary(
                peak_rss_bytes=1024,
                attribution_available=True,
            ),
        )


def _progress(frames: int, *, seconds: int) -> ProgressSnapshot:
    observed = NOW + timedelta(seconds=seconds)
    return ProgressSnapshot(
        phase=ProgressPhase.ENCODING,
        current=frames,
        total=100,
        unit=ProgressUnit.FRAMES,
        rate_per_second=None,
        speed_ratio=None,
        source=ProgressSource.AV1AN_STRUCTURED,
        message=None,
        phase_started_at=NOW,
        observed_at=observed,
        heartbeat_at=observed,
        advanced_at=observed,
    )
