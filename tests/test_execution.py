from __future__ import annotations

import os
import shlex
import threading
import time
from pathlib import Path

import pytest

from avarch.adapters.execution import (
    ProcessOutputRecord,
    _process_heartbeat_callback,  # pyright: ignore[reportPrivateUsage]
    _validate_mux_temporary_path,  # pyright: ignore[reportPrivateUsage]
    build_av1an_command,
    build_ffmpeg_mux_command,
    create_mux_temporary_path,
    execute_plan,
    preflight_execution,
    serialize_encoder_arguments,
    should_resume_av1an,
)
from avarch.adapters.filesystem.scanner import create_file_snapshot
from avarch.application.progress import NoopProgressSink, RecordingProgressSink
from avarch.domain.progress import ProgressPhase, ProgressSource
from avarch.models.execution import (
    ExecutionInterruptedError,
    ProcessCancellationToken,
    ToolUnavailableError,
    WorkDirectoryConflictError,
)
from avarch.models.plan import (
    AudioPlan,
    Av1anCommandSpec,
    ExecutionIdentity,
    ExecutionRuntimePaths,
    FfmpegMuxSpec,
    PlanArtifactPaths,
    SubtitlePlan,
    TranscodePlan,
    ValidationPolicy,
    VapourSynthPlan,
    VideoPlan,
)
from avarch.models.promotion import PromotionPolicy
from avarch.models.validation import DecodeSamplePolicy


@pytest.mark.parametrize(
    "arguments",
    [
        ["--preset", "6", "--crf", "28"],
        ["--metadata", "director's cut"],
        ["--custom", 'value "with quotes"'],
        ["--empty", ""],
        ["--path-like", r"C:\temporary folder\input"],
        ["--shell", "$(rm -rf nope); & | < >"],
        [
            "--preset",
            "6",
            "--metadata",
            "director's cut",
            "--custom",
            'value "with quotes"',
            "--path-like",
            r"C:\temporary folder\input",
        ],
    ],
)
def test_encoder_arguments_are_reversible(arguments: list[str]) -> None:
    serialized = serialize_encoder_arguments(arguments)

    assert shlex.split(serialized) == arguments


def test_build_av1an_command_uses_fixed_contract_order(tmp_path: Path) -> None:
    spec = Av1anCommandSpec(
        input_path=tmp_path / "movie.vpy",
        video_output_path=tmp_path / "video-only.mkv",
        temp_dir=tmp_path / "av1an",
        working_directory=tmp_path,
        encoder="svt-av1",
        encoder_args=["--metadata", "director's cut", "--custom", 'value "quoted"'],
        workers=6,
    )

    command = build_av1an_command(spec, resume=True)

    assert command == [
        "av1an",
        "-i",
        str(spec.input_path),
        "-o",
        str(spec.video_output_path),
        "--temp",
        str(spec.temp_dir),
        "--encoder",
        "svt-av1",
        "--video-params",
        serialize_encoder_arguments(spec.encoder_args),
        "--workers",
        "6",
        "--pix-format",
        "yuv420p10le",
        "--concat",
        "ffmpeg",
        "--cache-mode",
        "temp",
        "--max-tries",
        "3",
        "--audio-params",
        "-an",
        "--no-defaults",
        "--keep",
        "-n",
        "--resume",
    ]


def test_should_resume_av1an_uses_nonempty_temp_dir(tmp_path: Path) -> None:
    spec = Av1anCommandSpec(
        input_path=tmp_path / "movie.vpy",
        video_output_path=tmp_path / "video-only.mkv",
        temp_dir=tmp_path / "av1an",
        working_directory=tmp_path,
        encoder="svt-av1",
        encoder_args=["--crf", "28"],
        workers=6,
    )

    assert should_resume_av1an(spec) is False
    spec.temp_dir.mkdir()
    assert should_resume_av1an(spec) is False
    (spec.temp_dir / "state.json").write_text("{}", encoding="utf-8")
    assert should_resume_av1an(spec) is True


def test_should_resume_av1an_rejects_file_temp_path(tmp_path: Path) -> None:
    temp_path = tmp_path / "av1an"
    temp_path.write_text("not a directory", encoding="utf-8")
    spec = Av1anCommandSpec(
        input_path=tmp_path / "movie.vpy",
        video_output_path=tmp_path / "video-only.mkv",
        temp_dir=temp_path,
        working_directory=tmp_path,
        encoder="svt-av1",
        encoder_args=["--crf", "28"],
        workers=6,
    )

    with pytest.raises(WorkDirectoryConflictError):
        should_resume_av1an(spec)


def test_build_ffmpeg_mux_command_maps_global_source_indexes(tmp_path: Path) -> None:
    spec = FfmpegMuxSpec(
        video_input_path=tmp_path / "video-only.mkv",
        source_input_path=tmp_path / "movie.mkv",
        output_path=tmp_path / "movie.av1.mkv",
        audio_stream_index=3,
        subtitle_stream_indexes=[4, 9],
        audio_codec="libopus",
        audio_bitrate="128k",
        audio_channels=2,
    )
    temporary_output = tmp_path / ".movie.av1.mkv.muxing-test.mkv"

    command = build_ffmpeg_mux_command(spec, temporary_output)

    assert command == [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "info",
        "-n",
        "-i",
        str(spec.video_input_path),
        "-i",
        str(spec.source_input_path),
        "-map",
        "0:v:0",
        "-map",
        "1:3",
        "-map",
        "1:4",
        "-map",
        "1:9",
        "-map_metadata",
        "1",
        "-map_chapters",
        "1",
        "-c:v",
        "copy",
        "-c:a",
        "libopus",
        "-b:a",
        "128k",
        "-ac:a:0",
        "2",
        "-c:s",
        "copy",
        str(temporary_output),
    ]


def test_build_ffmpeg_mux_command_allows_video_only_sources(tmp_path: Path) -> None:
    spec = FfmpegMuxSpec(
        video_input_path=tmp_path / "video-only.mkv",
        source_input_path=tmp_path / "movie.mkv",
        output_path=tmp_path / "movie.av1.mkv",
        audio_stream_index=None,
        subtitle_stream_indexes=[],
    )
    temporary_output = tmp_path / ".movie.av1.mkv.muxing-test.mkv"

    command = build_ffmpeg_mux_command(spec, temporary_output)

    assert "-c:a" not in command
    assert "1:None" not in command
    assert command == [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-loglevel",
        "info",
        "-n",
        "-i",
        str(spec.video_input_path),
        "-i",
        str(spec.source_input_path),
        "-map",
        "0:v:0",
        "-map_metadata",
        "1",
        "-map_chapters",
        "1",
        "-c:v",
        "copy",
        "-c:s",
        "copy",
        str(temporary_output),
    ]


def test_mux_temporary_path_accepts_relative_plan_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    plan = _sample_plan(tmp_path).model_copy(
        update={
            "temp_dir": Path("work"),
            "output_path": Path("work/movie.av1.mkv"),
        }
    )

    temporary_output = create_mux_temporary_path(plan.output_path)

    _validate_mux_temporary_path(plan, temporary_output)


def test_execute_plan_writes_markers_and_reuses_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_tools(tmp_path, monkeypatch)
    plan = _sample_plan(tmp_path)

    status = execute_plan(plan)

    assert status == "completed"
    assert plan.av1an.video_output_path.read_bytes() == b"video"
    assert plan.output_path.read_bytes() == b"final"
    assert plan.runtime.av1an_stage_marker.is_file()
    assert plan.runtime.encode_result.is_file()

    monkeypatch.setenv("PATH", str(tmp_path / "missing-tools"))

    assert execute_plan(plan) == "already_complete"


def test_execute_plan_accepts_progress_sink_without_requiring_adapter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_tools(tmp_path, monkeypatch)
    plan = _sample_plan(tmp_path)
    assert execute_plan(plan) == "completed"

    assert execute_plan(plan, progress_sink=NoopProgressSink()) == "already_complete"


def test_execute_plan_reports_encoding_before_av1an_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_tools(tmp_path, monkeypatch)
    plan = _sample_plan(tmp_path)
    sink = RecordingProgressSink()

    assert execute_plan(plan, progress_sink=sink) == "completed"

    assert [snapshot.phase for snapshot in sink.snapshots] == [
        ProgressPhase.ENCODING,
        ProgressPhase.MUXING,
    ]


def test_process_output_callback_publishes_heartbeat_without_advancement() -> None:
    sink = RecordingProgressSink()
    callback = _process_heartbeat_callback(sink, ProgressPhase.ENCODING)

    callback(ProcessOutputRecord(stream="stderr", data=b"frame\n", text="frame\n"))

    assert len(sink.snapshots) == 1
    snapshot = sink.snapshots[0]
    assert snapshot.phase == ProgressPhase.ENCODING
    assert snapshot.source == ProgressSource.PROCESS_HEARTBEAT
    assert snapshot.current is None
    assert snapshot.advanced_at is None
    assert snapshot.heartbeat_at == snapshot.observed_at


def test_execute_plan_emits_process_heartbeat_while_child_is_silent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("avarch.adapters.execution.PROCESS_HEARTBEAT_INTERVAL_SECONDS", 0.02)
    _install_fake_tools(tmp_path, monkeypatch, av1an_start_delay=0.08)
    plan = _sample_plan(tmp_path)
    sink = RecordingProgressSink()

    assert execute_plan(plan, progress_sink=sink) == "completed"

    heartbeats = [
        snapshot
        for snapshot in sink.snapshots
        if snapshot.source == ProgressSource.PROCESS_HEARTBEAT
        and snapshot.phase == ProgressPhase.ENCODING
    ]
    assert heartbeats
    assert all(snapshot.current is None for snapshot in heartbeats)
    assert all(snapshot.advanced_at is None for snapshot in heartbeats)


def test_execute_plan_emits_numeric_av1an_progress_from_child_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_tools(tmp_path, monkeypatch, av1an_progress=True)
    plan = _sample_plan(tmp_path)
    sink = RecordingProgressSink()

    assert execute_plan(plan, progress_sink=sink) == "completed"

    numeric = [
        snapshot
        for snapshot in sink.snapshots
        if snapshot.source == ProgressSource.AV1AN_OUTPUT and snapshot.current is not None
    ]
    assert [snapshot.current for snapshot in numeric] == [0.0, 120.0]
    assert numeric[-1].total == 120.0
    assert numeric[-1].unit is not None
    assert numeric[-1].rate_per_second == 60.0
    assert b"120/120" in plan.runtime.av1an_stderr_log.read_bytes()


def test_execute_plan_ignores_malformed_av1an_progress_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_tools(tmp_path, monkeypatch, av1an_progress=False, av1an_noise=True)
    plan = _sample_plan(tmp_path)
    sink = RecordingProgressSink()

    assert execute_plan(plan, progress_sink=sink) == "completed"

    assert not [
        snapshot
        for snapshot in sink.snapshots
        if snapshot.source == ProgressSource.AV1AN_OUTPUT
    ]


def test_execute_plan_falls_back_to_phase_progress_when_av1an_parser_disabled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_tools(tmp_path, monkeypatch, av1an_progress=True)
    monkeypatch.setenv("AVARCH_AV1AN_TTY_PROGRESS", "0")
    plan = _sample_plan(tmp_path)
    sink = RecordingProgressSink()

    assert execute_plan(plan, progress_sink=sink) == "completed"

    assert ProgressPhase.ENCODING in [snapshot.phase for snapshot in sink.snapshots]
    assert not [
        snapshot
        for snapshot in sink.snapshots
        if snapshot.source == ProgressSource.AV1AN_OUTPUT
    ]


def test_execute_plan_interrupts_managed_process_when_token_is_cancelled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_cancellable_fake_tools(tmp_path, monkeypatch)
    plan = _sample_plan(tmp_path)
    token = ProcessCancellationToken()
    error_holder: list[BaseException] = []
    completed = threading.Event()

    def run_plan() -> None:
        try:
            execute_plan(plan, cancellation_token=token)
        except BaseException as exc:
            error_holder.append(exc)
        finally:
            completed.set()

    thread = threading.Thread(target=run_plan)
    thread.start()
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if plan.runtime.av1an_stdout_log.exists() and "ready" in (
            plan.runtime.av1an_stdout_log.read_text(encoding="utf-8", errors="ignore")
        ):
            break
        time.sleep(0.02)

    token.request("test cancellation")
    thread.join(timeout=3.0)

    assert completed.is_set()
    assert any(isinstance(exc, ExecutionInterruptedError) for exc in error_holder)
    assert "terminated" in plan.runtime.av1an_stdout_log.read_text(encoding="utf-8")


def test_preflight_rejects_panicking_av1an_version_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_panicking_av1an(tmp_path, monkeypatch)
    plan = _sample_plan(tmp_path)

    with pytest.raises(ToolUnavailableError, match="Run vapoursynth config"):
        preflight_execution(plan)


def test_preflight_rejects_missing_ffmpeg_audio_decoder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_tools(tmp_path, monkeypatch, ffmpeg_decoders=["aac"])
    plan = _sample_plan(tmp_path)
    assert plan.audio is not None
    plan = plan.model_copy(update={"audio": plan.audio.model_copy(update={"source_codec": "eac3"})})

    with pytest.raises(ToolUnavailableError, match="decoder.*eac3"):
        preflight_execution(plan)


def test_preflight_rejects_missing_ffmpeg_audio_encoder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_tools(tmp_path, monkeypatch, ffmpeg_encoders=["aac"])
    plan = _sample_plan(tmp_path)

    with pytest.raises(ToolUnavailableError, match="encoder.*libopus"):
        preflight_execution(plan)


def _install_fake_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    ffmpeg_decoders: list[str] | None = None,
    ffmpeg_encoders: list[str] | None = None,
    av1an_progress: bool = False,
    av1an_noise: bool = False,
    av1an_start_delay: float = 0.0,
) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    av1an_start_delay_literal = "0" if av1an_start_delay == 0 else str(av1an_start_delay)
    av1an = bin_dir / "av1an"
    av1an.write_text(
        """#!/usr/bin/env bash
if [ "${1:-}" = "--version" ]; then
  echo "av1an 0.5.1"
  exit 0
fi
out=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-o" ]; then
    shift
    out="$1"
  fi
  shift
done
if [ "__AV1AN_PROGRESS__" = "yes" ]; then
  printf '00:00:00 [0/1 Chunks] 0/120 (0 fps, eta unknown)\\r' >&2
  printf '00:00:01 [0/1 Chunks] 120/120 (60 fps, eta 0s)\\r' >&2
fi
if [ "__AV1AN_NOISE__" = "yes" ]; then
  printf 'not really progress: maybe soon\\r' >&2
fi
if [ "__AV1AN_START_DELAY__" != "0" ]; then
  sleep "__AV1AN_START_DELAY__"
fi
printf video > "$out"
""".replace("__AV1AN_PROGRESS__", "yes" if av1an_progress else "no").replace(
            "__AV1AN_NOISE__",
            "yes" if av1an_noise else "no",
        ).replace(
            "__AV1AN_START_DELAY__",
            av1an_start_delay_literal,
        ),
        encoding="utf-8",
    )
    ffmpeg = bin_dir / "ffmpeg"
    decoder_lines = "\n".join(
        f" A....D {codec}                  fake decoder" for codec in (ffmpeg_decoders or ["aac"])
    )
    encoder_lines = "\n".join(
        f" A..... {codec}                  fake encoder"
        for codec in (ffmpeg_encoders or ["libopus"])
    )
    ffmpeg.write_text(
        f"""#!/usr/bin/env bash
if [ "${{1:-}}" = "-version" ]; then
  echo "ffmpeg version 6.1"
  exit 0
fi
if [ "${{1:-}}" = "-decoders" ]; then
  cat <<'EOF'
{decoder_lines}
EOF
  exit 0
fi
if [ "${{1:-}}" = "-encoders" ]; then
  cat <<'EOF'
{encoder_lines}
EOF
  exit 0
fi
out="${{@: -1}}"
printf final > "$out"
""",
        encoding="utf-8",
    )
    av1an.chmod(0o755)
    ffmpeg.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")


def _install_panicking_av1an(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    av1an = bin_dir / "av1an"
    av1an.write_text(
        """#!/usr/bin/env bash
echo "thread 'main' panicked at /cargo/vapoursynth-0.5.1/src/vsscript/mod.rs" >&2
echo "Failed to get VSScript API" >&2
exit 1
""",
        encoding="utf-8",
    )
    ffmpeg = bin_dir / "ffmpeg"
    ffmpeg.write_text(
        """#!/usr/bin/env bash
echo "ffmpeg version 6.1"
""",
        encoding="utf-8",
    )
    vspipe = bin_dir / "vspipe"
    vspipe.write_text(
        """#!/usr/bin/env bash
echo "Run vapoursynth config to set it for this Python installation" >&2
exit 1
""",
        encoding="utf-8",
    )
    av1an.chmod(0o755)
    ffmpeg.chmod(0o755)
    vspipe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ.get('PATH', '')}")


def _install_cancellable_fake_tools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_tools(tmp_path, monkeypatch)
    av1an = tmp_path / "bin" / "av1an"
    av1an.write_text(
        """#!/usr/bin/env bash
if [ "${1:-}" = "--version" ]; then
  echo "av1an 0.5.1"
  exit 0
fi
trap 'echo terminated; exit 0' TERM
echo ready
while true; do
  sleep 0.05
done
""",
        encoding="utf-8",
    )
    av1an.chmod(0o755)


def _sample_plan(tmp_path: Path) -> TranscodePlan:
    source = tmp_path / "movie.mkv"
    source.write_bytes(b"media")
    snapshot = create_file_snapshot(source)
    work_dir = tmp_path / ".avarch" / "work" / "key"
    artifact_dir = tmp_path / ".avarch" / "plans" / "key"
    runtime_dir = work_dir / "runtime"
    script_path = artifact_dir / "movie.vpy"
    video_output = work_dir / "video-only.mkv"
    final_output = work_dir / "movie.av1.mkv"
    return TranscodePlan(
        plan_hash="plan-hash",
        input_path=source.resolve(),
        output_path=final_output,
        temp_dir=work_dir,
        media_file_id=1,
        source_fs_fingerprint=snapshot.fs_fingerprint,
        profile_name="av1_1080p_sdr",
        profile_hash="profile-hash",
        probe_hash="probe-hash",
        video=VideoPlan(
            source_stream_index=0,
            source_codec="hevc",
            source_width=3840,
            source_height=2160,
            source_bit_depth=10,
            source_pix_fmt="yuv420p10le",
            source_color_transfer="bt709",
            source_color_primaries="bt709",
            source_color_space="bt709",
            source_hdr_metadata_present=False,
            max_width=1920,
            target_width=1920,
            target_height=1080,
            resize_required=True,
            hdr_to_sdr=True,
            source="vapoursynth",
        ),
        audio=AudioPlan(
            source_stream_index=1,
            source_codec="aac",
            source_language="eng",
            source_channels=6,
            source_title="Main",
            source_commentary=False,
            target_codec="libopus",
            target_bitrate="128k",
            target_channels=2,
        ),
        subtitles=SubtitlePlan(streams=[]),
        execution_identity=ExecutionIdentity(
            av1an_contract_version=2,
            ffmpeg_mux_contract_version=1,
            av1an_version_family="0.5.x",
            video_container="mkv",
            final_container="mkv",
            identity_hash="identity-hash",
        ),
        vapoursynth=VapourSynthPlan(
            mode="generated",
            script_path=script_path,
            source_path=source,
            source_stream_index=0,
            index_cache_dir=work_dir / "bestsource",
            target_width=1920,
            target_height=1080,
            source_pix_fmt="yuv420p10le",
            source_color_transfer="bt709",
            source_color_primaries="bt709",
            source_color_space="bt709",
            source_hdr_metadata_present=False,
            hdr_to_sdr=True,
            identity_hash="vapoursynth-identity",
        ),
        av1an=Av1anCommandSpec(
            input_path=script_path,
            video_output_path=video_output,
            temp_dir=work_dir / "av1an",
            working_directory=work_dir,
            encoder="svt-av1",
            encoder_args=["--crf", "28"],
            workers=2,
        ),
        mux=FfmpegMuxSpec(
            video_input_path=video_output,
            source_input_path=source.resolve(),
            output_path=final_output,
            audio_stream_index=1,
            subtitle_stream_indexes=[],
            audio_codec="libopus",
            audio_bitrate="128k",
            audio_channels=2,
        ),
        runtime=ExecutionRuntimePaths(
            runtime_dir=runtime_dir,
            av1an_stdout_log=runtime_dir / "av1an.stdout.log",
            av1an_stderr_log=runtime_dir / "av1an.stderr.log",
            mux_stdout_log=runtime_dir / "mux.stdout.log",
            mux_stderr_log=runtime_dir / "mux.stderr.log",
            av1an_stage_marker=runtime_dir / "av1an-stage.json",
            encode_result=runtime_dir / "encode-result.json",
            validation_report=runtime_dir / "validation-report.json",
            validation_decode_stdout_log=runtime_dir / "validation.decode.stdout.log",
            validation_decode_stderr_log=runtime_dir / "validation.decode.stderr.log",
        ),
        validation=ValidationPolicy(
            policy_hash="policy-hash",
            accepted_container_names=["matroska,webm"],
            source_duration_seconds=600.0,
            source_size_bytes=1024,
            duration_tolerance_seconds=2.0,
            expected_width=1920,
            expected_height=1080,
            expected_audio_codec="opus",
            expected_audio_channels=2,
            expected_audio_language="eng",
            expected_subtitles=[],
            minimum_output_bytes=1024,
            minimum_output_source_ratio=0.01,
            minimum_size_reduction_percent=None,
            decode_sample=DecodeSamplePolicy(enabled=False, duration_seconds=5.0),
        ),
        promotion=PromotionPolicy(policy_hash="promotion-policy-hash"),
        artifacts=PlanArtifactPaths(
            artifact_dir=artifact_dir,
            plan_json=artifact_dir / "plan.json",
            vapoursynth_script=script_path,
            av1an_command_json=artifact_dir / "av1an.command.json",
            validation_policy_json=artifact_dir / "validation-policy.json",
        ),
    )
