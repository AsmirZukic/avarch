from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol


class CommandRunner(Protocol):
    def __call__(
        self,
        command: Sequence[str],
        *,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True, slots=True)
class NvencCapability:
    available: bool
    gpu_available: bool
    runtime_accessible: bool
    encoder_available: bool
    pixel_format_supported: bool
    encoder_initialized: bool
    gpu_names: tuple[str, ...]
    reason: str | None = None


def detect_nvenc_capability(
    *,
    ffmpeg_executable: str = "ffmpeg",
    encoder: str = "av1_nvenc",
    gpu: int = 0,
    pixel_format: str = "p010le",
    runner: CommandRunner | None = None,
) -> NvencCapability:
    run = runner or _run_command
    if shutil.which(ffmpeg_executable) is None:
        return _unavailable("FFmpeg is not available on PATH")
    if shutil.which("nvidia-smi") is None:
        return _unavailable("NVIDIA driver utilities are not available")

    smi = run(("nvidia-smi", "--query-gpu=name", "--format=csv,noheader"), timeout=15)
    gpu_names = tuple(line.strip() for line in smi.stdout.splitlines() if line.strip())
    if smi.returncode != 0:
        return NvencCapability(
            available=False,
            gpu_available=False,
            runtime_accessible=False,
            encoder_available=False,
            pixel_format_supported=False,
            encoder_initialized=False,
            gpu_names=(),
            reason=_diagnostic("NVIDIA driver/runtime is not accessible", smi),
        )
    if not gpu_names:
        return _unavailable("No NVIDIA GPU was reported by nvidia-smi")
    if gpu >= len(gpu_names):
        return NvencCapability(
            available=False,
            gpu_available=True,
            runtime_accessible=True,
            encoder_available=False,
            pixel_format_supported=False,
            encoder_initialized=False,
            gpu_names=gpu_names,
            reason=f"Configured GPU index {gpu} does not exist; detected {len(gpu_names)} GPU(s)",
        )

    encoders = run((ffmpeg_executable, "-hide_banner", "-encoders"), timeout=15)
    if encoders.returncode != 0 or not _encoder_listed(encoders.stdout, encoders.stderr, encoder):
        return NvencCapability(
            available=False,
            gpu_available=True,
            runtime_accessible=True,
            encoder_available=False,
            pixel_format_supported=False,
            encoder_initialized=False,
            gpu_names=gpu_names,
            reason=f"FFmpeg encoder {encoder} is not available",
        )

    help_result = run(
        (ffmpeg_executable, "-hide_banner", "-h", f"encoder={encoder}"),
        timeout=15,
    )
    help_text = f"{help_result.stdout}\n{help_result.stderr}"
    pixel_supported = help_result.returncode == 0 and pixel_format in help_text
    if not pixel_supported:
        return NvencCapability(
            available=False,
            gpu_available=True,
            runtime_accessible=True,
            encoder_available=True,
            pixel_format_supported=False,
            encoder_initialized=False,
            gpu_names=gpu_names,
            reason=f"FFmpeg {encoder} does not advertise pixel format {pixel_format}",
        )

    initialize = run(
        (
            ffmpeg_executable,
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=size=64x64:rate=1:duration=1",
            "-frames:v",
            "1",
            "-pix_fmt",
            pixel_format,
            "-c:v",
            encoder,
            "-gpu",
            str(gpu),
            "-preset",
            "p5",
            "-tune",
            "hq",
            "-rc",
            "vbr",
            "-cq",
            "28",
            "-f",
            "null",
            "-",
        ),
        timeout=30,
    )
    if initialize.returncode != 0:
        return NvencCapability(
            available=False,
            gpu_available=True,
            runtime_accessible=True,
            encoder_available=True,
            pixel_format_supported=True,
            encoder_initialized=False,
            gpu_names=gpu_names,
            reason=_diagnostic(f"FFmpeg could not initialize {encoder} on GPU {gpu}", initialize),
        )

    return NvencCapability(
        available=True,
        gpu_available=True,
        runtime_accessible=True,
        encoder_available=True,
        pixel_format_supported=True,
        encoder_initialized=True,
        gpu_names=gpu_names,
    )


def _run_command(
    command: Sequence[str],
    *,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(command),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return subprocess.CompletedProcess(list(command), 127, "", str(exc))


def _encoder_listed(stdout: str, stderr: str, encoder: str) -> bool:
    return any(
        line.split()[1:2] == [encoder]
        for line in f"{stdout}\n{stderr}".splitlines()
        if line.strip()
    )


def _diagnostic(prefix: str, result: subprocess.CompletedProcess[str]) -> str:
    detail = (result.stderr or result.stdout).strip()
    if not detail:
        return prefix
    lines = detail.splitlines()
    return f"{prefix}: {' | '.join(lines[-12:])}"


def _unavailable(reason: str) -> NvencCapability:
    return NvencCapability(
        available=False,
        gpu_available=False,
        runtime_accessible=False,
        encoder_available=False,
        pixel_format_supported=False,
        encoder_initialized=False,
        gpu_names=(),
        reason=reason,
    )
