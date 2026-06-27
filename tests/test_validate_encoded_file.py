from __future__ import annotations

from pathlib import Path
from typing import Any

from avarch.adapters.probe import ProbeProcessError
from avarch.adapters.validation import validate_encoded_file
from avarch.profiles.models import (
    EncodingProfile,
    ProfileAudioSettings,
    ProfileAv1anSettings,
    ProfileMatchSettings,
    ProfileSubtitleSettings,
    ProfileVideoSettings,
)


def test_validation_fails_when_output_missing(tmp_path: Path) -> None:
    original = tmp_path / "movie.mkv"
    encoded = tmp_path / "movie.av1.mkv"
    original.write_bytes(b"original")

    report = validate_encoded_file(original, encoded, _profile(), probe_runner=_probe_runner({}))

    checks = {check.name: check for check in report.checks}
    assert report.passed is False
    assert checks["output_exists"].status.value == "fail"
    assert checks["ffprobe_readable"].status.value == "skipped"


def test_validation_fails_when_ffprobe_fails(tmp_path: Path) -> None:
    original = tmp_path / "movie.mkv"
    encoded = tmp_path / "movie.av1.mkv"
    original.write_bytes(b"original")
    encoded.write_bytes(b"encoded")

    def runner(path: Path) -> dict[str, Any]:
        if path == encoded:
            raise ProbeProcessError("ffprobe could not read output")
        return _probe_payload(codec="hevc")

    report = validate_encoded_file(original, encoded, _profile(), probe_runner=runner)

    checks = {check.name: check for check in report.checks}
    assert report.passed is False
    assert checks["ffprobe_readable"].status.value == "fail"
    assert checks["ffprobe_readable"].message == "ffprobe could not read output"


def test_validation_fails_when_duration_diff_too_large(tmp_path: Path) -> None:
    original = tmp_path / "movie.mkv"
    encoded = tmp_path / "movie.av1.mkv"
    original.write_bytes(b"original")
    encoded.write_bytes(b"encoded")

    report = validate_encoded_file(
        original,
        encoded,
        _profile(),
        probe_runner=_probe_runner(
            {
                original: _probe_payload(codec="hevc", duration="600.0"),
                encoded: _probe_payload(codec="av1", duration="610.5"),
            }
        ),
    )

    checks = {check.name: check for check in report.checks}
    assert report.passed is False
    assert checks["duration_close"].status.value == "fail"


def test_validation_passes_for_valid_av1_mkv(tmp_path: Path) -> None:
    original = tmp_path / "movie.mkv"
    encoded = tmp_path / "movie.av1.mkv"
    original.write_bytes(b"original")
    encoded.write_bytes(b"encoded")

    report = validate_encoded_file(
        original,
        encoded,
        _profile(),
        probe_runner=_probe_runner(
            {
                original: _probe_payload(codec="hevc", duration="600.0"),
                encoded: _probe_payload(codec="av1", duration="600.8"),
            }
        ),
    )

    assert report.passed is True


def _profile() -> EncodingProfile:
    return EncodingProfile(
        backend="av1an",
        container="mkv",
        match=ProfileMatchSettings(video_codec_not=["av1"]),
        video=ProfileVideoSettings(max_width=1920),
        av1an=ProfileAv1anSettings(encoder="svt-av1", workers=1, video_args="--crf 30"),
        audio=ProfileAudioSettings(codec="opus", bitrate="128k", channels=2, languages=["eng"]),
        subtitles=ProfileSubtitleSettings(languages=["eng"]),
    )


def _probe_runner(payloads: dict[Path, dict[str, Any]]) -> Any:
    def runner(path: Path) -> dict[str, Any]:
        return payloads[path]

    return runner


def _probe_payload(*, codec: str, duration: str = "600.0") -> dict[str, Any]:
    return {
        "streams": [
            {
                "index": 0,
                "codec_name": codec,
                "codec_type": "video",
                "width": 1920,
                "height": 1080,
            }
        ],
        "format": {"format_name": "matroska,webm", "duration": duration},
    }