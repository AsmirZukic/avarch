from __future__ import annotations

import math
import shutil
import subprocess
from pathlib import Path

import pytest

from avarch.adapters.replacement_validation import (
    ReplacementValidationError,
    validate_replacement_media,
)


@pytest.fixture
def real_media(tmp_path: Path) -> Path:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("FFmpeg and ffprobe required for real-media regression")
    path = tmp_path / "complete.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=64x64:rate=10:duration=6",
            "-c:v",
            "libaom-av1",
            "-cpu-used",
            "8",
            "-threads",
            "1",
            "-crf",
            "40",
            str(path),
        ],
        check=True,
        timeout=60,
    )
    return path


def test_complete_av1_has_sixty_decoded_frames(real_media: Path) -> None:
    evidence = validate_replacement_media(real_media, real_media, duration_seconds=6)
    assert evidence["decoded_video_frames"] == 60
    span = evidence["output_span_seconds"]
    assert isinstance(span, float) and math.isclose(span, 5.9)


def test_truncated_av1_with_original_container_duration_is_rejected(real_media: Path) -> None:
    truncated = real_media.with_name("truncated.mkv")
    data = real_media.read_bytes()
    truncated.write_bytes(data[: len(data) // 2])
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(truncated),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert math.isclose(float(probe.stdout), 6)
    with pytest.raises(ReplacementValidationError):
        validate_replacement_media(real_media, truncated, duration_seconds=6)


def test_readable_shorter_video_is_rejected(real_media: Path) -> None:
    shortened = real_media.with_name("short.mkv")
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(real_media),
            "-t",
            "3",
            "-c:v",
            "libaom-av1",
            "-cpu-used",
            "8",
            "-threads",
            "1",
            str(shortened),
        ],
        check=True,
        timeout=60,
    )
    with pytest.raises(ReplacementValidationError, match="frame counts"):
        validate_replacement_media(real_media, shortened, duration_seconds=6)


@pytest.mark.parametrize("audio_seconds", [3, 6])
def test_audio_coverage_is_checked_independently_of_video_duration(
    real_media: Path,
    audio_seconds: int,
) -> None:
    paths: list[Path] = []
    for name, seconds in (("source-audio", 6), ("output-audio", audio_seconds)):
        path = real_media.with_name(name + ".mkv")
        subprocess.run(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-i",
                str(real_media),
                "-f",
                "lavfi",
                "-i",
                f"sine=frequency=440:duration={seconds}",
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "copy",
                "-c:a",
                "libopus",
                str(path),
            ],
            check=True,
            timeout=60,
        )
        paths.append(path)
    if audio_seconds == 3:
        with pytest.raises(ReplacementValidationError, match="audio timestamp coverage"):
            validate_replacement_media(paths[0], paths[1], duration_seconds=6, source_audio_index=1)
    else:
        evidence = validate_replacement_media(
            paths[0],
            paths[1],
            duration_seconds=6,
            source_audio_index=1,
        )
        assert evidence["audio_coverage"] is not None


def test_same_frame_count_with_changed_timestamps_is_rejected(real_media: Path) -> None:
    retimed = real_media.with_name("retimed.mkv")
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-i",
            str(real_media),
            "-vf",
            "setpts=2*PTS",
            "-fps_mode",
            "passthrough",
            "-c:v",
            "libaom-av1",
            "-cpu-used",
            "8",
            "-threads",
            "1",
            str(retimed),
        ],
        check=True,
        timeout=60,
    )
    with pytest.raises(ReplacementValidationError, match="timestamp coverage"):
        validate_replacement_media(real_media, retimed, duration_seconds=6)
