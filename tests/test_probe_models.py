from pathlib import Path

from pydantic import ValidationError

from avarch.models.probe import AudioStream, NormalizedProbe, VideoStream
from avarch.probe import canonical_json, format_probe_summary


def test_normalized_probe_defaults_stream_lists() -> None:
    probe = NormalizedProbe()

    assert probe.video_streams == []
    assert probe.audio_streams == []
    assert probe.subtitle_streams == []
    assert probe.attachments == []
    assert probe.chapters == []


def test_video_stream_accepts_fractional_fps() -> None:
    stream = VideoStream(index=0, fps="24000/1001")

    assert stream.fps == "24000/1001"


def test_audio_stream_contains_policy_relevant_fields() -> None:
    stream = AudioStream(
        index=1,
        codec="eac3",
        language="eng",
        channels=6,
        title="Director Commentary",
        default=True,
        forced=False,
        commentary=True,
    )

    assert stream.commentary is True


def test_probe_model_serializes_deterministically() -> None:
    probe = NormalizedProbe(container="matroska,webm", duration_seconds=600.0)

    assert canonical_json(probe) == (
        '{"attachments":[],"audio_streams":[],"bitrate_bps":null,"chapters":[],'
        '"container":"matroska,webm","duration_seconds":600.0,"schema_version":1,'
        '"subtitle_streams":[],"video_streams":[]}'
    )


def test_video_stream_rejects_negative_index() -> None:
    try:
        VideoStream(index=-1)
    except ValidationError:
        return

    raise AssertionError("negative stream indexes must be rejected")


def test_format_probe_summary_handles_missing_values() -> None:
    summary = format_probe_summary(
        path=Path("/media/missing.mkv"),
        normalized=NormalizedProbe(video_streams=[VideoStream(index=0)]),
        probe_hash="abc",
    )

    assert "Container: -" in summary
    assert "[0] - - - fps - -" in summary
