from __future__ import annotations

import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from avarch.adapters import nvenc as nvenc_module
from avarch.adapters.execution import build_nvenc_ffmpeg_command, build_vspipe_command
from avarch.adapters.nvenc import detect_nvenc_capability
from avarch.adapters.progress.ffmpeg import FfmpegProgressParser
from avarch.models.plan import NvencCommandSpec, NvencToneMappingSpec


def _spec(tmp_path: Path) -> NvencCommandSpec:
    return NvencCommandSpec(
        input_path=tmp_path / "movie with spaces.vpy",
        video_output_path=tmp_path / "video only.mkv",
        working_directory=tmp_path,
        encoder="av1_nvenc",
        preset="p5",
        cq=28,
        gpu=0,
    )


def test_nvenc_commands_are_argument_safe_and_use_y4m_pipe(tmp_path: Path) -> None:
    spec = _spec(tmp_path)

    assert build_vspipe_command(spec) == [
        "vspipe",
        "-c",
        "y4m",
        str(spec.input_path),
        "-",
    ]
    command = build_nvenc_ffmpeg_command(spec)

    assert command[0] == "ffmpeg"
    assert command[6:10] == ["-f", "yuv4mpegpipe", "-i", "pipe:0"]
    assert command[command.index("-c:v") + 1] == "av1_nvenc"
    assert command[command.index("-pix_fmt") + 1] == "p010le"
    assert command[command.index("-progress") + 1] == "pipe:1"
    assert command[-1] == str(spec.video_output_path)
    assert " ".join(command) not in command


def test_nvenc_opencl_tone_mapping_command_uses_gpu_filter(tmp_path: Path) -> None:
    base = _spec(tmp_path)
    spec = base.model_copy(
        update={
            "tone_mapping": NvencToneMappingSpec(
                source_color_primaries="bt2020",
                source_color_transfer="smpte2084",
                source_color_space="bt2020nc",
            )
        }
    )

    command = build_nvenc_ffmpeg_command(spec)

    assert command[command.index("-init_hw_device") + 1] == "opencl=ocl:0.0"
    assert command[command.index("-filter_hw_device") + 1] == "ocl"
    filter_graph = command[command.index("-vf") + 1]
    assert "setparams=color_primaries=bt2020:color_trc=smpte2084" in filter_graph
    assert "tonemap_opencl=tonemap=mobius" in filter_graph
    assert "transfer=bt709:matrix=bt709:primaries=bt709:range=tv" in filter_graph
    assert filter_graph.endswith("hwdownload,format=p010le")


def test_ffmpeg_progress_parser_handles_partial_records() -> None:
    parser = FfmpegProgressParser()

    assert parser.feed(b"frame=120\nfps=59.5\nout_time_us=200") == ()
    samples = parser.feed(
        b"0000\nbitrate=2200.4kbits/s\ntotal_size=5500000\nspeed=2.5x\nprogress=continue\n"
    )

    assert len(samples) == 1
    sample = samples[0]
    assert sample.frame == 120
    assert sample.fps == 59.5
    assert sample.encoded_seconds == 2.0
    assert sample.bitrate_kbps == 2200
    assert sample.written_bytes == 5_500_000
    assert sample.speed_ratio == 2.5
    assert sample.completed is False


def test_nvenc_capability_requires_successful_encoder_initialization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def which(_name: str) -> str:
        return "/usr/bin/tool"

    monkeypatch.setattr(nvenc_module.shutil, "which", which)

    def runner(
        command: Sequence[str],
        *,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        args = tuple(command)
        if args[0] == "nvidia-smi":
            return subprocess.CompletedProcess(args, 0, "NVIDIA RTX\n", "")
        if "-encoders" in args:
            return subprocess.CompletedProcess(args, 0, " V....D av1_nvenc NVIDIA NVENC\n", "")
        if "encoder=av1_nvenc" in args:
            return subprocess.CompletedProcess(
                args,
                0,
                "Supported pixel formats: yuv420p p010le cuda\n",
                "",
            )
        return subprocess.CompletedProcess(args, 1, "", "driver does not support NVENC API")

    capability = detect_nvenc_capability(runner=runner)

    assert capability.available is False
    assert capability.encoder_available is True
    assert capability.pixel_format_supported is True
    assert capability.encoder_initialized is False
    assert capability.reason is not None
    assert "could not initialize" in capability.reason


def test_nvenc_capability_reports_available_after_real_init_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def which(_name: str) -> str:
        return "/usr/bin/tool"

    monkeypatch.setattr(nvenc_module.shutil, "which", which)

    def runner(
        command: Sequence[str],
        *,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        del timeout
        args = tuple(command)
        if args[0] == "nvidia-smi":
            return subprocess.CompletedProcess(args, 0, "NVIDIA RTX\n", "")
        if "-encoders" in args:
            return subprocess.CompletedProcess(args, 0, " V....D av1_nvenc NVIDIA NVENC\n", "")
        if "encoder=av1_nvenc" in args:
            return subprocess.CompletedProcess(args, 0, "p010le\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    capability = detect_nvenc_capability(runner=runner)

    assert capability.available is True
    assert capability.gpu_names == ("NVIDIA RTX",)
