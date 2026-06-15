from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from pydantic import BaseModel, ValidationError

from avarch.models.execution import (
    Av1anStageError,
    ExecutionInterruptedError,
    InvalidExecutionPlanError,
    MuxStageError,
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
from avarch.planner import SUPPORTED_AV1AN_VERSION_FAMILY
from avarch.scanner import create_file_snapshot
from avarch.serialization import canonical_json

MAX_PROCESS_TAIL_BYTES = 16_384


def serialize_encoder_arguments(arguments: Sequence[str]) -> str:
    return shlex.join(arguments)


def should_resume_av1an(spec: Av1anCommandSpec) -> bool:
    if spec.resume_policy == "never":
        return False
    if not spec.temp_dir.exists():
        return False
    if not spec.temp_dir.is_dir():
        raise WorkDirectoryConflictError(f"Av1an temp path is not a directory: {spec.temp_dir}")
    try:
        next(spec.temp_dir.iterdir())
    except StopIteration:
        return False
    except FileNotFoundError:
        return False
    except NotADirectoryError as exc:
        raise WorkDirectoryConflictError(
            f"Av1an temp path is not a directory: {spec.temp_dir}"
        ) from exc
    except PermissionError as exc:
        raise WorkDirectoryConflictError(
            f"Unable to inspect Av1an temp directory: {spec.temp_dir}"
        ) from exc
    return True


def build_av1an_command(spec: Av1anCommandSpec, *, resume: bool | None = None) -> list[str]:
    if resume is None:
        resume = should_resume_av1an(spec)

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
        "--workers",
        str(spec.workers),
        "--pix-format",
        spec.pixel_format,
        "--concat",
        spec.concat_method,
        "--cache-mode",
        spec.cache_mode,
        "--max-tries",
        str(spec.max_tries),
        "--audio-params",
        "-an",
        "--no-defaults",
        "--keep",
        "-n",
    ]
    if resume:
        command.append("--resume")
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
        "-map",
        f"1:{spec.audio_stream_index}",
    ]
    for stream_index in spec.subtitle_stream_indexes:
        command.extend(["-map", f"1:{stream_index}"])
    command.extend(
        [
            "-map_metadata",
            "1",
            "-map_chapters",
            "1",
            "-c:v",
            "copy",
            "-c:a",
            spec.audio_codec,
            "-b:a",
            spec.audio_bitrate,
            "-ac:a:0",
            str(spec.audio_channels),
            "-c:s",
            "copy",
            _command_path(temporary_output),
        ]
    )
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
    payload = b"av1an-spec-v1\0" + canonical_json(spec).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=32).hexdigest()


def build_mux_spec_hash(spec: FfmpegMuxSpec) -> str:
    payload = b"ffmpeg-mux-spec-v1\0" + canonical_json(spec).encode("utf-8")
    return hashlib.blake2b(payload, digest_size=32).hexdigest()


def build_process_tail(data: bytes, *, max_bytes: int = MAX_PROCESS_TAIL_BYTES) -> str:
    tail = data[-max_bytes:]
    if len(data) > max_bytes:
        tail = b"... truncated ...\n" + tail
    return tail.decode("utf-8", errors="replace")


def parse_av1an_progress(text: str) -> float | None:
    matches = re.findall(r"(?<!\d)(100(?:\.0+)?|[1-9]?\d(?:\.\d+)?)%", text)
    if not matches:
        return None
    return float(matches[-1])


def execute_plan(plan: TranscodePlan) -> EncodeExecutionStatus:
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
        command = build_av1an_command(plan.av1an)
        exit_code = _run_process(
            command,
            cwd=plan.av1an.working_directory,
            stdout_log=plan.runtime.av1an_stdout_log,
            stderr_log=plan.runtime.av1an_stderr_log,
            plan_hash=plan.plan_hash,
            command_hash=_command_hash(command),
        )
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
        exit_code = _run_process(
            command,
            cwd=plan.temp_dir,
            stdout_log=plan.runtime.mux_stdout_log,
            stderr_log=plan.runtime.mux_stderr_log,
            plan_hash=plan.plan_hash,
            command_hash=_command_hash(command),
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
    match = re.search(r"\bav1an\D+(\d+)\.(\d+)(?:\.\d+)?", version_text, re.IGNORECASE)
    if match is None:
        raise UnsupportedToolVersionError("Unable to detect Av1an version.")
    major, minor = match.groups()
    if f"{major}.{minor}.x" != SUPPORTED_AV1AN_VERSION_FAMILY:
        raise UnsupportedToolVersionError(
            f"Unsupported Av1an version family {major}.{minor}.x; "
            f"expected {SUPPORTED_AV1AN_VERSION_FAMILY}."
        )


def _preflight_ffmpeg_version(executable: str) -> None:
    _tool_version_output(executable, version_args=("-version",))


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


def _run_process(
    command: Sequence[str],
    *,
    cwd: Path,
    stdout_log: Path,
    stderr_log: Path,
    plan_hash: str,
    command_hash: str,
) -> int:
    stdout_log.parent.mkdir(parents=True, exist_ok=True)
    stderr_log.parent.mkdir(parents=True, exist_ok=True)
    started_at = _utc_now().isoformat()
    start_separator = (
        "=== avarch process start ===\n"
        f"started_at={started_at}\n"
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
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            stdout=stdout_file,
            stderr=stderr_file,
            stdin=subprocess.DEVNULL,
        )
        try:
            exit_code = process.wait()
        except KeyboardInterrupt as exc:
            interrupted = True
            process.terminate()
            try:
                exit_code = process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                exit_code = process.wait()
            _write_process_end(stdout_file, stderr_file, exit_code=exit_code, interrupted=True)
            raise ExecutionInterruptedError(f"Interrupted while running {command[0]}") from exc
        _write_process_end(stdout_file, stderr_file, exit_code=exit_code, interrupted=interrupted)
    return exit_code


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
        completed_at=_utc_now(),
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
        completed_at=_utc_now(),
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
