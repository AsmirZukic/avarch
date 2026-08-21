from __future__ import annotations

import errno
import hashlib
import os
import pty
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, Literal, Protocol, cast

from pydantic import BaseModel, ValidationError

from avarch.adapters.filesystem.scanner import create_file_snapshot
from avarch.adapters.progress.av1an_tty import (
    Av1anTtyProgressParser,
    Av1anTtyProgressSample,
    av1an_tty_progress_supported,
)
from avarch.adapters.system.process_telemetry import LinuxProcessTelemetryObserver
from avarch.application.planning import SUPPORTED_AV1AN_VERSION_FAMILY
from avarch.application.progress import ProgressSink, publish_progress_safely
from avarch.contracts import AV1AN_SPEC_HASH_CONTRACT, FFMPEG_MUX_SPEC_HASH_CONTRACT
from avarch.domain.progress import ProgressPhase, ProgressSnapshot, ProgressSource
from avarch.models.execution import (
    Av1anStageError,
    ExecutionInterruptedError,
    InvalidExecutionPlanError,
    MuxStageError,
    ProcessCancellationToken,
    ProcessFailureReason,
    ProcessResourceSummary,
    ProcessResult,
    ProcessTerminationReason,
    ResourceExhaustionError,
    StaleExecutionPlanError,
    ToolUnavailableError,
    UnsupportedToolVersionError,
    WorkDirectoryConflictError,
)
from avarch.models.plan import (
    AV1AN_COMMAND_CONTRACT_VERSION,
    FFMPEG_MUX_CONTRACT_VERSION,
    Av1anCommandSpec,
    Av1anStageMarker,
    EncodeExecutionStatus,
    EncodeResultReceipt,
    FfmpegMuxSpec,
    TranscodePlan,
)
from avarch.serialization import canonical_json

MAX_PROCESS_TAIL_BYTES = 16_384
PROCESS_HEARTBEAT_INTERVAL_SECONDS = 5.0


class _ManagedProcessRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._processes: dict[int, subprocess.Popen[bytes]] = {}
        self._paused = False

    def register(self, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            self._processes[process.pid] = process
            paused = self._paused
        if paused:
            _signal_process_group(process, signal.SIGSTOP)

    def unregister(self, process: subprocess.Popen[bytes]) -> None:
        with self._lock:
            self._processes.pop(process.pid, None)

    def pause(self) -> None:
        with self._lock:
            self._paused = True
            processes = self._live_processes_locked()
        for process in processes:
            _signal_process_group(process, signal.SIGSTOP)

    def resume(self) -> None:
        with self._lock:
            self._paused = False
            processes = self._live_processes_locked()
        for process in processes:
            _signal_process_group(process, signal.SIGCONT)

    def _live_processes_locked(self) -> tuple[subprocess.Popen[bytes], ...]:
        finished = [pid for pid, process in self._processes.items() if process.poll() is not None]
        for pid in finished:
            self._processes.pop(pid, None)
        return tuple(self._processes.values())


_MANAGED_PROCESSES = _ManagedProcessRegistry()


def pause_managed_processes() -> None:
    if os.name == "posix":
        _MANAGED_PROCESSES.pause()


def resume_managed_processes() -> None:
    if os.name == "posix":
        _MANAGED_PROCESSES.resume()


@dataclass(frozen=True, slots=True)
class ProcessOutputRecord:
    stream: Literal["stdout", "stderr"]
    data: bytes
    text: str


ProcessOutputCallback = Callable[[ProcessOutputRecord], None]


class ProcessTelemetryObserver(Protocol):
    def before_start(self) -> None: ...

    def sample(self, root_pid: int) -> None: ...

    def after_exit(self, root_pid: int | None) -> ProcessResourceSummary: ...


def serialize_encoder_arguments(arguments: Sequence[str]) -> str:
    return shlex.join(arguments)


def build_av1an_command(spec: Av1anCommandSpec) -> list[str]:
    command = [
        spec.executable,
        "-i",
        _command_path(spec.input_path),
        "-o",
        _command_path(spec.video_output_path),
        "--temp",
        _command_path(spec.temp_dir),
        "--encoder",
        spec.encoder,
        "--video-params",
        serialize_encoder_arguments(spec.encoder_args),
    ]
    if spec.workers != "auto":
        command.extend(["--workers", str(spec.workers)])
    command.extend(
        [
            "--pix-format",
            spec.pixel_format,
            "--concat",
            spec.concat_method,
            "--max-tries",
            str(spec.max_tries),
            "--audio-params",
            "-an",
            "--no-defaults",
            "-n",
        ]
    )
    return command


def build_ffmpeg_mux_command(spec: FfmpegMuxSpec, temporary_output: Path) -> list[str]:
    command = [
        spec.executable,
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "info",
        "-n",
        "-i",
        _command_path(spec.video_input_path),
        "-i",
        _command_path(spec.source_input_path),
        "-map",
        "0:v:0",
    ]
    if spec.audio_stream_index is not None:
        command.extend(["-map", f"1:{spec.audio_stream_index}"])
    for stream_index in spec.subtitle_stream_indexes:
        command.extend(["-map", f"1:{stream_index}"])
    command.extend(["-map_metadata", "1", "-map_chapters", "1", "-c:v", "copy"])
    if spec.audio_stream_index is not None:
        if spec.audio_codec is None or spec.audio_bitrate is None or spec.audio_channels is None:
            raise InvalidExecutionPlanError("Mux audio settings are incomplete.")
        command.extend(
            [
                "-c:a",
                spec.audio_codec,
                "-b:a",
                spec.audio_bitrate,
                "-ac:a:0",
                str(spec.audio_channels),
            ]
        )
    command.extend(["-c:s", "copy", _command_path(temporary_output)])
    return command


def create_mux_temporary_path(final_output: Path) -> Path:
    final_output.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(
        dir=final_output.parent,
        prefix=f".{final_output.name}.muxing-",
        suffix=".mkv",
    )
    os.close(fd)
    temporary_output = Path(raw_path)
    temporary_output.unlink()
    return temporary_output


def build_av1an_spec_hash(spec: Av1anCommandSpec) -> str:
    payload = f"{AV1AN_SPEC_HASH_CONTRACT}\0".encode() + canonical_json(spec).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=32).hexdigest()


def build_mux_spec_hash(spec: FfmpegMuxSpec) -> str:
    payload = f"{FFMPEG_MUX_SPEC_HASH_CONTRACT}\0".encode() + canonical_json(spec).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=32).hexdigest()


def build_process_tail(data: bytes, *, max_bytes: int = MAX_PROCESS_TAIL_BYTES) -> str:
    tail = data[-max_bytes:]
    if len(data) > max_bytes:
        tail = b"... truncated ...\n" + tail
    return tail.decode("utf-8", errors="replace")


def execute_plan(
    plan: TranscodePlan,
    *,
    cancellation_token: ProcessCancellationToken | None = None,
    progress_sink: ProgressSink | None = None,
) -> EncodeExecutionStatus:
    av1an_spec_hash = build_av1an_spec_hash(plan.av1an)
    mux_spec_hash = build_mux_spec_hash(plan.mux)

    if _valid_encode_receipt(plan, av1an_spec_hash=av1an_spec_hash, mux_spec_hash=mux_spec_hash):
        return "already_complete"
    if plan.output_path.exists():
        raise WorkDirectoryConflictError(
            f"Final output exists without a valid execution receipt: {plan.output_path}"
        )

    preflight_execution(plan)

    plan.temp_dir.mkdir(parents=True, exist_ok=True)
    plan.runtime.runtime_dir.mkdir(parents=True, exist_ok=True)

    if _valid_av1an_marker(plan, av1an_spec_hash=av1an_spec_hash):
        video_output_size = plan.av1an.video_output_path.stat().st_size
    else:
        if plan.av1an.video_output_path.exists():
            raise WorkDirectoryConflictError(
                "Video intermediate exists without a valid Av1an stage marker: "
                f"{plan.av1an.video_output_path}"
            )
        plan.av1an.working_directory.mkdir(parents=True, exist_ok=True)
        _reset_av1an_temp_dir(plan.av1an.temp_dir)
        command = build_av1an_command(plan.av1an)
        _publish_execution_phase(progress_sink, ProgressPhase.SCENE_DETECTION)
        av1an_progress = _make_av1an_progress_collector(plan, progress_sink)
        exit_code = _run_process(
            command,
            cwd=plan.av1an.working_directory,
            stdout_log=plan.runtime.av1an_stdout_log,
            stderr_log=plan.runtime.av1an_stderr_log,
            plan_hash=plan.plan_hash,
            command_hash=_command_hash(command),
            cancellation_token=cancellation_token,
            progress_sink=progress_sink,
            progress_phase=(
                av1an_progress.current_phase
                if av1an_progress is not None
                else ProgressPhase.SCENE_DETECTION
            ),
            output_callback=av1an_progress.callback if av1an_progress is not None else None,
            stderr_tty=av1an_progress is not None,
        )
        if av1an_progress is not None:
            av1an_progress.flush()
        if exit_code != 0:
            tail = _read_tail(plan.runtime.av1an_stderr_log)
            raise Av1anStageError(f"Av1an failed with exit code {exit_code}.\n{tail}")
        video_output_size = _require_nonempty_file(
            plan.av1an.video_output_path,
            label="Av1an video output",
        )
        _write_av1an_marker(
            plan,
            av1an_spec_hash=av1an_spec_hash,
            video_output_size=video_output_size,
        )

    temporary_output = create_mux_temporary_path(plan.output_path)
    _validate_mux_temporary_path(plan, temporary_output)
    command = build_ffmpeg_mux_command(plan.mux, temporary_output)
    try:
        _publish_execution_phase(progress_sink, ProgressPhase.MUXING)
        exit_code = _run_process(
            command,
            cwd=plan.temp_dir,
            stdout_log=plan.runtime.mux_stdout_log,
            stderr_log=plan.runtime.mux_stderr_log,
            plan_hash=plan.plan_hash,
            command_hash=_command_hash(command),
            cancellation_token=cancellation_token,
            progress_sink=progress_sink,
            progress_phase=ProgressPhase.MUXING,
        )
        if exit_code != 0:
            tail = _read_tail(plan.runtime.mux_stderr_log)
            raise MuxStageError(f"FFmpeg mux failed with exit code {exit_code}.\n{tail}")
        final_output_size = _require_nonempty_file(
            temporary_output,
            label="temporary mux output",
        )
        os.replace(temporary_output, plan.output_path)
    finally:
        if temporary_output.exists():
            temporary_output.unlink()

    _write_encode_receipt(
        plan,
        av1an_spec_hash=av1an_spec_hash,
        mux_spec_hash=mux_spec_hash,
        video_output_size=video_output_size,
        final_output_size=final_output_size,
    )
    return "completed"


def _reset_av1an_temp_dir(temp_dir: Path) -> None:
    if not temp_dir.exists():
        return
    if not temp_dir.is_dir():
        raise WorkDirectoryConflictError(f"Av1an temp path is not a directory: {temp_dir}")
    shutil.rmtree(temp_dir)


def _publish_execution_phase(
    progress_sink: ProgressSink | None,
    phase: ProgressPhase,
) -> None:
    now = _utc_now()
    publish_progress_safely(
        progress_sink,
        ProgressSnapshot(
            phase=phase,
            current=None,
            total=None,
            unit=None,
            rate_per_second=None,
            speed_ratio=None,
            source=ProgressSource.SCHEDULER,
            message=None,
            phase_started_at=now,
            observed_at=now,
            heartbeat_at=now,
            advanced_at=None,
        ),
    )


def _make_av1an_progress_collector(
    plan: TranscodePlan,
    progress_sink: ProgressSink | None,
) -> _Av1anProgressCollector | None:
    if progress_sink is None:
        return None
    if not av1an_tty_progress_supported(
        plan.execution_identity.av1an_version_family,
        enabled=os.environ.get("AVARCH_AV1AN_TTY_PROGRESS", "1") != "0",
    ):
        return None
    return _Av1anProgressCollector(progress_sink, Av1anTtyProgressParser())


def preflight_execution(plan: TranscodePlan) -> None:
    if plan.execution_identity.av1an_contract_version != AV1AN_COMMAND_CONTRACT_VERSION:
        raise InvalidExecutionPlanError("Plan Av1an command contract is unsupported.")
    if plan.execution_identity.ffmpeg_mux_contract_version != FFMPEG_MUX_CONTRACT_VERSION:
        raise InvalidExecutionPlanError("Plan FFmpeg mux contract is unsupported.")
    if plan.execution_identity.av1an_version_family != SUPPORTED_AV1AN_VERSION_FAMILY:
        raise InvalidExecutionPlanError("Plan Av1an version family is unsupported.")

    _preflight_source_fingerprint(plan)
    _validate_plan_path_safety(plan)
    _preflight_tool(plan.av1an.executable, required=True)
    _preflight_tool(plan.mux.executable, required=True)
    _preflight_av1an_version(plan.av1an.executable)
    _preflight_ffmpeg_version(plan.mux.executable)
    _preflight_ffmpeg_audio_codecs(plan)


def _preflight_source_fingerprint(plan: TranscodePlan) -> None:
    try:
        snapshot = create_file_snapshot(plan.input_path)
    except OSError as exc:
        raise StaleExecutionPlanError(f"Unable to inspect source file: {plan.input_path}") from exc

    # This mirrors scanner freshness semantics. The fingerprint is stat metadata,
    # not a content hash; network filesystems may expose stale or low-precision attrs.
    if snapshot.fs_fingerprint != plan.source_fs_fingerprint:
        raise StaleExecutionPlanError(
            "The source file no longer matches the planned filesystem fingerprint. "
            "This freshness check uses path, size, mtime_ns, device, and inode metadata; "
            "it is not a cryptographic content hash. Run avarch scan and avarch probe again."
        )


def _validate_plan_path_safety(plan: TranscodePlan) -> None:
    work_dir = plan.temp_dir
    runtime_dir = plan.runtime.runtime_dir
    paths_that_must_differ = [
        plan.input_path,
        plan.vapoursynth.script_path,
        plan.av1an.video_output_path,
        plan.output_path,
    ]
    if len({_resolved(path) for path in paths_that_must_differ}) != len(paths_that_must_differ):
        raise InvalidExecutionPlanError(
            "Input, script, video output, and final output must differ."
        )

    if plan.output_path == plan.input_path:
        raise InvalidExecutionPlanError("Output path must not equal source path.")
    for label, path in (
        ("final output", plan.output_path),
        ("video output", plan.av1an.video_output_path),
        ("Av1an temp", plan.av1an.temp_dir),
        ("Av1an working directory", plan.av1an.working_directory),
        ("runtime directory", runtime_dir),
    ):
        if not _is_relative_to(path, work_dir):
            raise InvalidExecutionPlanError(
                f"{label} path must be under the work directory: {path}"
            )

    for label, path in (
        ("Av1an stdout log", plan.runtime.av1an_stdout_log),
        ("Av1an stderr log", plan.runtime.av1an_stderr_log),
        ("mux stdout log", plan.runtime.mux_stdout_log),
        ("mux stderr log", plan.runtime.mux_stderr_log),
        ("Av1an marker", plan.runtime.av1an_stage_marker),
        ("encode receipt", plan.runtime.encode_result),
    ):
        if not _is_relative_to(path, runtime_dir):
            raise InvalidExecutionPlanError(
                f"{label} path must be under the runtime directory: {path}"
            )


def _validate_mux_temporary_path(plan: TranscodePlan, temporary_output: Path) -> None:
    if temporary_output.exists():
        raise WorkDirectoryConflictError(f"Mux temporary output already exists: {temporary_output}")
    if temporary_output == plan.output_path:
        raise InvalidExecutionPlanError("Mux temporary output must differ from final output.")
    if temporary_output == plan.input_path or temporary_output == plan.av1an.video_output_path:
        raise InvalidExecutionPlanError(
            "Mux temporary output must differ from source and video input."
        )
    if _resolved(temporary_output.parent) != _resolved(plan.output_path.parent):
        raise InvalidExecutionPlanError("Mux temporary output must be beside the final output.")
    if not _is_relative_to(temporary_output, plan.temp_dir):
        raise InvalidExecutionPlanError("Mux temporary output must be under the work directory.")


def _preflight_tool(executable: str, *, required: bool) -> None:
    if required and shutil.which(executable) is None:
        raise ToolUnavailableError(f"Required executable is not available on PATH: {executable}")


def _preflight_av1an_version(executable: str) -> None:
    try:
        version_text = _tool_version_output(executable)
    except ToolUnavailableError as exc:
        diagnostic = _vapoursynth_diagnostic()
        if diagnostic:
            raise ToolUnavailableError(f"{exc}\n\nVapourSynth diagnostic:\n{diagnostic}") from exc
        raise
    version = parse_av1an_version(version_text)
    major, minor, *_rest = version.split(".")
    if f"{major}.{minor}.x" != SUPPORTED_AV1AN_VERSION_FAMILY:
        raise UnsupportedToolVersionError(
            f"Unsupported Av1an version family {major}.{minor}.x; "
            f"expected {SUPPORTED_AV1AN_VERSION_FAMILY}."
        )


def _preflight_ffmpeg_version(executable: str) -> None:
    _tool_version_output(executable, version_args=("-version",))


def _preflight_ffmpeg_audio_codecs(plan: TranscodePlan) -> None:
    if plan.audio is None:
        return
    source_codec = _normalize_codec_name(plan.audio.source_codec)
    if source_codec is not None:
        decoders = _ffmpeg_codec_names(plan.mux.executable, "-decoders")
        if source_codec not in decoders:
            raise ToolUnavailableError(
                "Planned audio transcode requires an FFmpeg decoder that is not available: "
                f"{source_codec}. Install an FFmpeg build with this decoder, or use a source "
                "audio codec supported by the configured FFmpeg."
            )

    target_codec = _normalize_codec_name(plan.audio.target_codec)
    encoders = _ffmpeg_codec_names(plan.mux.executable, "-encoders")
    if target_codec not in encoders:
        raise ToolUnavailableError(
            "Planned audio transcode requires an FFmpeg encoder that is not available: "
            f"{target_codec}. Install an FFmpeg build with this encoder, or choose a supported "
            "profile audio codec."
        )


def _ffmpeg_codec_names(executable: str, argument: str) -> set[str]:
    output = _tool_version_output(executable, version_args=(argument,))
    names: set[str] = set()
    for line in output.splitlines():
        match = re.match(r"\s*[A-Z.]{6,7}\s+(\S+)\s+", line)
        if match is not None:
            names.add(match.group(1).strip().lower())
    if not names:
        raise ToolUnavailableError(f"{executable} {argument} produced no codec listing.")
    return names


def _normalize_codec_name(codec: str | None) -> str | None:
    if codec is None:
        return None
    normalized = codec.strip().lower()
    return normalized or None


def _vapoursynth_diagnostic() -> str | None:
    if shutil.which("vspipe") is None:
        return "vspipe is not available on PATH."
    try:
        completed = subprocess.run(
            ["vspipe", "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = f"{completed.stdout}\n{completed.stderr}".strip()
    if not output:
        return None
    return build_process_tail(output.encode("utf-8"))


def _tool_version_output(
    executable: str,
    *,
    version_args: Sequence[str] = ("--version",),
) -> str:
    try:
        completed = subprocess.run(
            [executable, *version_args],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except OSError as exc:
        raise ToolUnavailableError(f"Unable to execute required tool: {executable}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ToolUnavailableError(f"Timed out while checking tool version: {executable}") from exc
    output = f"{completed.stdout}\n{completed.stderr}"
    if completed.returncode != 0:
        tail = build_process_tail(output.encode("utf-8"))
        rendered_args = " ".join(version_args)
        raise ToolUnavailableError(
            f"{executable} {rendered_args} failed with exit code {completed.returncode}.\n{tail}"
        )
    if not output.strip():
        rendered_args = " ".join(version_args)
        raise ToolUnavailableError(f"{executable} {rendered_args} produced no output.")
    return output


def parse_av1an_version(output: str) -> str:
    match = re.search(r"\bav1an\D+(\d+\.\d+\.\d+(?:[-+][\w.\-]+)?)\b", output, re.IGNORECASE)
    if match is None:
        raise UnsupportedToolVersionError("Unable to detect Av1an version.")
    return match.group(1)


def _run_process(
    command: Sequence[str],
    *,
    cwd: Path,
    stdout_log: Path,
    stderr_log: Path,
    plan_hash: str,
    command_hash: str,
    cancellation_token: ProcessCancellationToken | None = None,
    progress_sink: ProgressSink | None = None,
    progress_phase: ProgressPhase | Callable[[], ProgressPhase] | None = None,
    output_callback: ProcessOutputCallback | None = None,
    stderr_tty: bool = False,
    process_observer: ProcessTelemetryObserver | None = None,
) -> int:
    heartbeat_callback = (
        _process_heartbeat_callback(progress_sink, progress_phase)
        if progress_sink is not None and progress_phase is not None
        else None
    )
    callback = _combined_process_output_callback(heartbeat_callback, output_callback)
    periodic_heartbeat_stop = threading.Event()
    periodic_heartbeat = (
        _start_periodic_process_heartbeat(
            progress_sink,
            progress_phase,
            stop_event=periodic_heartbeat_stop,
        )
        if progress_sink is not None and progress_phase is not None
        else None
    )
    try:
        result = run_managed_process(
            command,
            cwd=cwd,
            stdout_log=stdout_log,
            stderr_log=stderr_log,
            plan_hash=plan_hash,
            command_hash=command_hash,
            stdout_callback=callback,
            stderr_callback=callback,
            cancellation_token=cancellation_token,
            stderr_tty=stderr_tty,
            process_observer=(
                process_observer if process_observer is not None else _default_process_observer()
            ),
        )
    finally:
        periodic_heartbeat_stop.set()
        if periodic_heartbeat is not None:
            periodic_heartbeat.join()
    _publish_resource_summary(progress_sink, result.resource_summary)
    if result.termination_reason is not ProcessTerminationReason.EXITED:
        raise ExecutionInterruptedError(f"Interrupted while running {command[0]}")
    if result.failure_reason is ProcessFailureReason.RESOURCE_OOM:
        raise ResourceExhaustionError(
            f"{command[0]} failed due to resource exhaustion (cgroup OOM kill).",
            resource_summary=result.resource_summary,
        )
    return result.return_code


def _publish_resource_summary(
    progress_sink: ProgressSink | None,
    summary: ProcessResourceSummary | None,
) -> None:
    if progress_sink is None or summary is None:
        return
    callback = getattr(progress_sink, "record_resource_summary", None)
    if callable(callback):
        with suppress(Exception):
            callback(summary)


def _default_process_observer() -> ProcessTelemetryObserver | None:
    if os.name != "posix":
        return None
    return LinuxProcessTelemetryObserver()


def _combined_process_output_callback(
    first: ProcessOutputCallback | None,
    second: ProcessOutputCallback | None,
) -> ProcessOutputCallback | None:
    callbacks = tuple(callback for callback in (first, second) if callback is not None)
    if not callbacks:
        return None

    def callback(record: ProcessOutputRecord) -> None:
        for process_callback in callbacks:
            with suppress(Exception):
                process_callback(record)

    return callback


class _Av1anProgressCollector:
    def __init__(
        self,
        progress_sink: ProgressSink,
        parser: Av1anTtyProgressParser,
    ) -> None:
        self._progress_sink = progress_sink
        self._parser = parser
        self._phase = ProgressPhase.SCENE_DETECTION
        self._lock = threading.Lock()

    def callback(self, record: ProcessOutputRecord) -> None:
        if record.stream != "stderr":
            return
        with self._lock:
            for sample in self._parser.feed(record.data):
                self._publish_if_accepted(sample)

    def flush(self) -> None:
        with self._lock:
            for sample in self._parser.flush():
                self._publish_if_accepted(sample)

    def current_phase(self) -> ProgressPhase:
        with self._lock:
            return self._phase

    def _publish_if_accepted(self, sample: Av1anTtyProgressSample) -> None:
        if self._phase == ProgressPhase.ENCODING and sample.phase == ProgressPhase.SCENE_DETECTION:
            return
        if sample.phase == ProgressPhase.ENCODING:
            self._phase = ProgressPhase.ENCODING
        _publish_av1an_sample(self._progress_sink, sample)


def _publish_av1an_sample(
    progress_sink: ProgressSink,
    sample: Av1anTtyProgressSample,
) -> None:
    now = _utc_now()
    publish_progress_safely(
        progress_sink,
        ProgressSnapshot(
            phase=sample.phase,
            current=float(sample.current) if sample.current is not None else None,
            total=float(sample.total) if sample.total is not None else None,
            unit=sample.unit,
            rate_per_second=sample.rate_per_second,
            speed_ratio=sample.speed_ratio,
            source=ProgressSource.AV1AN_OUTPUT,
            message=sample.message,
            phase_started_at=now,
            observed_at=now,
            heartbeat_at=now,
            advanced_at=now,
            chunks_current=sample.chunks_current,
            chunks_total=sample.chunks_total,
            bitrate_kbps=sample.bitrate_kbps,
            estimated_output_bytes=sample.estimated_output_bytes,
        ),
    )


def _process_heartbeat_callback(
    progress_sink: ProgressSink,
    phase: ProgressPhase | Callable[[], ProgressPhase],
) -> ProcessOutputCallback:
    def callback(record: ProcessOutputRecord) -> None:
        del record
        _publish_process_heartbeat(progress_sink, phase)

    return callback


def _start_periodic_process_heartbeat(
    progress_sink: ProgressSink,
    phase: ProgressPhase | Callable[[], ProgressPhase],
    *,
    stop_event: threading.Event,
) -> threading.Thread:
    thread = threading.Thread(
        target=_publish_periodic_process_heartbeat,
        kwargs={
            "progress_sink": progress_sink,
            "phase": phase,
            "stop_event": stop_event,
        },
        daemon=True,
    )
    thread.start()
    return thread


def _publish_periodic_process_heartbeat(
    *,
    progress_sink: ProgressSink,
    phase: ProgressPhase | Callable[[], ProgressPhase],
    stop_event: threading.Event,
) -> None:
    while not stop_event.wait(PROCESS_HEARTBEAT_INTERVAL_SECONDS):
        _publish_process_heartbeat(progress_sink, phase)


def _publish_process_heartbeat(
    progress_sink: ProgressSink,
    phase: ProgressPhase | Callable[[], ProgressPhase],
) -> None:
    now = _utc_now()
    resolved_phase = phase() if callable(phase) else phase
    publish_progress_safely(
        progress_sink,
        ProgressSnapshot(
            phase=resolved_phase,
            current=None,
            total=None,
            unit=None,
            rate_per_second=None,
            speed_ratio=None,
            source=ProgressSource.PROCESS_HEARTBEAT,
            message=None,
            phase_started_at=now,
            observed_at=now,
            heartbeat_at=now,
            advanced_at=None,
        ),
    )


def run_managed_process(
    command: Sequence[str],
    *,
    cwd: Path,
    stdout_log: Path,
    stderr_log: Path,
    plan_hash: str,
    command_hash: str,
    stdout_callback: ProcessOutputCallback | None = None,
    stderr_callback: ProcessOutputCallback | None = None,
    cancellation_token: ProcessCancellationToken | None = None,
    termination_grace_seconds: float = 10.0,
    stderr_tty: bool = False,
    process_observer: ProcessTelemetryObserver | None = None,
) -> ProcessResult:
    stdout_log.parent.mkdir(parents=True, exist_ok=True)
    stderr_log.parent.mkdir(parents=True, exist_ok=True)
    started_at = _utc_now()
    start_separator = (
        "=== avarch process start ===\n"
        f"started_at={started_at.isoformat()}\n"
        f"plan_hash={plan_hash}\n"
        f"command_hash={command_hash}\n"
        f"executable={command[0]}\n"
    )
    with (
        stdout_log.open("ab") as stdout_file,
        stderr_log.open("ab") as stderr_file,
    ):
        for log_file in (stdout_file, stderr_file):
            log_file.write(start_separator.encode("utf-8"))
            log_file.flush()
        interrupted = False
        stderr_master_fd: int | None = None
        stderr_slave_fd: int | None = None
        stderr_target: int | object = subprocess.PIPE
        if stderr_tty and os.name == "posix":
            stderr_master_fd, stderr_slave_fd = pty.openpty()
            stderr_target = stderr_slave_fd
        try:
            if process_observer is not None:
                process_observer.before_start()
            process = subprocess.Popen(
                list(command),
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=stderr_target,
                stdin=subprocess.DEVNULL,
                start_new_session=(os.name == "posix"),
            )
            _MANAGED_PROCESSES.register(process)
        finally:
            if stderr_slave_fd is not None:
                os.close(stderr_slave_fd)
        reader_errors: list[BaseException] = []
        readers: list[threading.Thread] = []
        if process.stdout is not None:
            readers.append(
                threading.Thread(
                    target=_stream_process_output_thread,
                    kwargs={
                        "pipe": cast(BinaryIO, process.stdout),
                        "log_file": stdout_file,
                        "stream": "stdout",
                        "callback": stdout_callback,
                        "errors": reader_errors,
                    },
                    daemon=False,
                )
            )
        if stderr_master_fd is not None:
            readers.append(
                threading.Thread(
                    target=_stream_process_output_thread,
                    kwargs={
                        "pipe": os.fdopen(stderr_master_fd, "rb", buffering=0),
                        "log_file": stderr_file,
                        "stream": "stderr",
                        "callback": stderr_callback,
                        "errors": reader_errors,
                    },
                    daemon=False,
                )
            )
        elif process.stderr is not None:
            readers.append(
                threading.Thread(
                    target=_stream_process_output_thread,
                    kwargs={
                        "pipe": cast(BinaryIO, process.stderr),
                        "log_file": stderr_file,
                        "stream": "stderr",
                        "callback": stderr_callback,
                        "errors": reader_errors,
                    },
                    daemon=False,
                )
            )
        for reader in readers:
            reader.start()
        cancelled = False
        forced_kill = False
        termination_requested_at: datetime | None = None
        try:
            while True:
                if process_observer is not None:
                    process_observer.sample(process.pid)
                exit_code = process.poll()
                if exit_code is not None:
                    break
                if cancellation_token is not None and cancellation_token.cancel_requested:
                    cancelled = True
                    termination_requested_at = _utc_now()
                    _terminate_process(process)
                    try:
                        exit_code = process.wait(timeout=termination_grace_seconds)
                    except subprocess.TimeoutExpired:
                        forced_kill = True
                        _kill_process(process)
                        exit_code = process.wait()
                    break
                if cancellation_token is None:
                    try:
                        exit_code = process.wait(timeout=0.05)
                        break
                    except subprocess.TimeoutExpired:
                        continue
                cancellation_token.wait(timeout_seconds=0.05)
            for reader in readers:
                reader.join()
            if reader_errors:
                _MANAGED_PROCESSES.unregister(process)
                raise reader_errors[0]
        except KeyboardInterrupt as exc:
            interrupted = True
            _terminate_process(process)
            try:
                exit_code = process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                _kill_process(process)
                exit_code = process.wait()
            for reader in readers:
                reader.join()
            _write_process_end(stdout_file, stderr_file, exit_code=exit_code, interrupted=True)
            _MANAGED_PROCESSES.unregister(process)
            raise ExecutionInterruptedError(f"Interrupted while running {command[0]}") from exc
        _write_process_end(stdout_file, stderr_file, exit_code=exit_code, interrupted=interrupted)
        _MANAGED_PROCESSES.unregister(process)
    result_command = tuple(command)
    finished_at = _utc_now()
    resource_summary = (
        process_observer.after_exit(process.pid) if process_observer is not None else None
    )
    if forced_kill:
        return ProcessResult.killed(
            command=result_command,
            return_code=exit_code,
            started_at=started_at,
            finished_at=finished_at,
            termination_requested_at=termination_requested_at,
            resource_summary=resource_summary,
        )
    if cancelled:
        return ProcessResult.cancelled(
            command=result_command,
            return_code=exit_code,
            started_at=started_at,
            finished_at=finished_at,
            termination_requested_at=termination_requested_at,
            resource_summary=resource_summary,
        )
    return ProcessResult.exited(
        command=result_command,
        return_code=exit_code,
        started_at=started_at,
        finished_at=finished_at,
        resource_summary=resource_summary,
    )


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
            return
        except ProcessLookupError:
            return
    process.terminate()


def _signal_process_group(process: subprocess.Popen[bytes], signum: signal.Signals) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        return


def _kill_process(process: subprocess.Popen[bytes]) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except ProcessLookupError:
            return
    process.kill()


def _stream_process_output_thread(
    *,
    pipe: BinaryIO,
    log_file: BinaryIO,
    stream: Literal["stdout", "stderr"],
    callback: ProcessOutputCallback | None,
    errors: list[BaseException],
) -> None:
    try:
        _stream_process_output(pipe, log_file, stream=stream, callback=callback)
    except BaseException as exc:
        errors.append(exc)
    finally:
        with suppress(Exception):
            pipe.close()


def _stream_process_output(
    pipe: BinaryIO,
    log_file: BinaryIO,
    *,
    stream: Literal["stdout", "stderr"],
    callback: ProcessOutputCallback | None,
) -> None:
    pending = bytearray()
    while True:
        try:
            chunk = pipe.read(1)
        except OSError as exc:
            if exc.errno == errno.EIO:
                break
            raise
        if not chunk:
            break
        log_file.write(chunk)
        log_file.flush()
        pending.extend(chunk)
        if chunk in {b"\n", b"\r"}:
            _publish_process_output(stream=stream, data=bytes(pending), callback=callback)
            pending.clear()
    if pending:
        _publish_process_output(stream=stream, data=bytes(pending), callback=callback)


def _publish_process_output(
    *,
    stream: Literal["stdout", "stderr"],
    data: bytes,
    callback: ProcessOutputCallback | None,
) -> None:
    if callback is None:
        return
    callback(
        ProcessOutputRecord(
            stream=stream,
            data=data,
            text=data.decode("utf-8", errors="replace"),
        )
    )


def _write_process_end(
    stdout_file: BinaryIO,
    stderr_file: BinaryIO,
    *,
    exit_code: int,
    interrupted: bool,
) -> None:
    finished_at = _utc_now().isoformat()
    end_separator = (
        "=== avarch process end ===\n"
        f"finished_at={finished_at}\n"
        f"exit_code={exit_code}\n"
        f"interrupted={'true' if interrupted else 'false'}\n"
    )
    for log_file in (stdout_file, stderr_file):
        log_file.write(end_separator.encode("utf-8"))
        log_file.flush()


def _valid_av1an_marker(plan: TranscodePlan, *, av1an_spec_hash: str) -> bool:
    marker = _read_model(plan.runtime.av1an_stage_marker, Av1anStageMarker)
    if marker is None:
        return False
    if marker.plan_hash != plan.plan_hash or marker.av1an_spec_hash != av1an_spec_hash:
        return False
    if marker.video_output_path != plan.av1an.video_output_path:
        return False
    try:
        stat_result = plan.av1an.video_output_path.stat()
    except OSError:
        return False
    return stat_result.st_size == marker.video_output_size and stat_result.st_size > 0


def _valid_encode_receipt(
    plan: TranscodePlan,
    *,
    av1an_spec_hash: str,
    mux_spec_hash: str,
) -> bool:
    receipt = _read_model(plan.runtime.encode_result, EncodeResultReceipt)
    if receipt is None:
        return False
    if receipt.plan_hash != plan.plan_hash:
        return False
    if receipt.av1an_spec_hash != av1an_spec_hash or receipt.mux_spec_hash != mux_spec_hash:
        return False
    if receipt.final_output_path != plan.output_path:
        return False
    try:
        stat_result = plan.output_path.stat()
    except OSError:
        return False
    return stat_result.st_size == receipt.final_output_size and stat_result.st_size > 0


def _write_av1an_marker(
    plan: TranscodePlan,
    *,
    av1an_spec_hash: str,
    video_output_size: int,
) -> None:
    marker = Av1anStageMarker(
        plan_hash=plan.plan_hash,
        av1an_spec_hash=av1an_spec_hash,
        video_output_path=plan.av1an.video_output_path,
        video_output_size=video_output_size,
    )
    _write_json_atomically(plan.runtime.av1an_stage_marker, marker)


def _write_encode_receipt(
    plan: TranscodePlan,
    *,
    av1an_spec_hash: str,
    mux_spec_hash: str,
    video_output_size: int,
    final_output_size: int,
) -> None:
    receipt = EncodeResultReceipt(
        plan_hash=plan.plan_hash,
        av1an_spec_hash=av1an_spec_hash,
        mux_spec_hash=mux_spec_hash,
        video_output_path=plan.av1an.video_output_path,
        video_output_size=video_output_size,
        final_output_path=plan.output_path,
        final_output_size=final_output_size,
    )
    _write_json_atomically(plan.runtime.encode_result, receipt)


def _write_json_atomically(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.tmp-",
        suffix=".json",
        text=True,
    )
    temp_path = Path(raw_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as output_file:
            output_file.write(canonical_json(value))
            output_file.write("\n")
            output_file.flush()
            os.fsync(output_file.fileno())
        os.replace(temp_path, path)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def _read_model[ModelT: BaseModel](path: Path, model_type: type[ModelT]) -> ModelT | None:
    if not path.is_file():
        return None
    try:
        return model_type.model_validate_json(path.read_text(encoding="utf-8"))  # type: ignore[attr-defined]
    except (OSError, ValidationError, ValueError):
        return None


def _require_nonempty_file(path: Path, *, label: str) -> int:
    try:
        stat_result = path.stat()
    except OSError as exc:
        raise WorkDirectoryConflictError(f"{label} was not created: {path}") from exc
    if not path.is_file() or stat_result.st_size <= 0:
        raise WorkDirectoryConflictError(f"{label} is empty or not a regular file: {path}")
    return stat_result.st_size


def _read_tail(path: Path) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    return build_process_tail(data)


def _command_hash(command: Sequence[str]) -> str:
    payload = b"process-command-v1\0" + canonical_json(list(command)).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=32).hexdigest()


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        _resolved(path).relative_to(_resolved(root))
    except ValueError:
        return False
    return True


def _resolved(path: Path) -> Path:
    return path.resolve(strict=False)


def _command_path(path: Path) -> str:
    return str(_resolved(path))


def _utc_now() -> datetime:
    return datetime.now(UTC)
