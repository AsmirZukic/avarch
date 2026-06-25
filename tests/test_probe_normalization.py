from __future__ import annotations

from pathlib import Path

import pytest

from avarch.adapters.probe import ProbeNormalizationError, format_probe_summary, normalize_probe
from avarch.models.probe import AudioStream, NormalizedProbe, SubtitleStream, VideoStream


def test_normalize_container_metadata() -> None:
    probe = normalize_probe(
        {"format": {"format_name": "matroska,webm", "duration": "600.5", "bit_rate": "1234"}}
    )

    assert probe.container == "matroska,webm"
    assert probe.duration_seconds == 600.5
    assert probe.bitrate_bps == 1234


def test_normalize_video_stream() -> None:
    probe = normalize_probe(
        {
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "codec_name": "hevc",
                    "width": 3840,
                    "height": 2160,
                    "avg_frame_rate": "24000/1001",
                    "pix_fmt": "yuv420p10le",
                    "color_space": "bt2020nc",
                    "color_transfer": "smpte2084",
                    "color_primaries": "bt2020",
                }
            ]
        }
    )

    stream = probe.video_streams[0]
    assert stream.codec == "hevc"
    assert stream.width == 3840
    assert stream.height == 2160
    assert stream.fps == "24000/1001"
    assert stream.bit_depth == 10


def test_normalize_prefers_average_frame_rate() -> None:
    probe = normalize_probe(
        {
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "avg_frame_rate": "25/1",
                    "r_frame_rate": "30000/1001",
                }
            ]
        }
    )

    assert probe.video_streams[0].fps == "25/1"


def test_normalize_reduces_frame_rate_fraction() -> None:
    probe = normalize_probe(
        {"streams": [{"index": 0, "codec_type": "video", "avg_frame_rate": "50/2"}]}
    )

    assert probe.video_streams[0].fps == "25/1"


def test_normalize_rejects_zero_frame_rate() -> None:
    probe = normalize_probe(
        {"streams": [{"index": 0, "codec_type": "video", "avg_frame_rate": "0/0"}]}
    )

    assert probe.video_streams[0].fps is None


def test_normalize_bit_depth_from_raw_sample() -> None:
    probe = normalize_probe(
        {
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "pix_fmt": "yuv420p10le",
                    "bits_per_raw_sample": "12",
                }
            ]
        }
    )

    assert probe.video_streams[0].bit_depth == 12


def test_normalize_bit_depth_from_pixel_format() -> None:
    probe = normalize_probe(
        {"streams": [{"index": 0, "codec_type": "video", "pix_fmt": "yuv444p12le"}]}
    )

    assert probe.video_streams[0].bit_depth == 12


def test_normalize_hdr_metadata_presence() -> None:
    probe = normalize_probe(
        {
            "streams": [
                {
                    "index": 0,
                    "codec_type": "video",
                    "side_data_list": [{"side_data_type": "Mastering display metadata"}],
                }
            ]
        }
    )

    assert probe.video_streams[0].hdr_metadata_present is True


def test_normalize_audio_stream() -> None:
    probe = normalize_probe(
        {
            "streams": [
                {
                    "index": 1,
                    "codec_type": "audio",
                    "codec_name": "eac3",
                    "channels": "6",
                    "tags": {"language": " ENG ", "title": "Main Audio"},
                    "disposition": {"default": "1", "forced": 0},
                }
            ]
        }
    )

    assert probe.audio_streams[0].language == "eng"
    assert probe.audio_streams[0].default is True


def test_normalize_audio_commentary_from_title() -> None:
    probe = normalize_probe(
        {
            "streams": [
                {
                    "index": 1,
                    "codec_type": "audio",
                    "tags": {"title": "Director Commentary"},
                }
            ]
        }
    )

    assert probe.audio_streams[0].commentary is True


def test_audio_without_commentary_title_is_not_commentary() -> None:
    probe = normalize_probe(
        {"streams": [{"index": 1, "codec_type": "audio", "tags": {"title": "Director"}}]}
    )

    assert probe.audio_streams[0].commentary is False


def test_normalize_subtitle_stream() -> None:
    probe = normalize_probe(
        {
            "streams": [
                {
                    "index": 3,
                    "codec_type": "subtitle",
                    "codec_name": "subrip",
                    "tags": {"language": "eng", "title": "Forced"},
                    "disposition": {"forced": True},
                }
            ]
        }
    )

    assert probe.subtitle_streams[0].forced is True


def test_streams_are_sorted_by_index() -> None:
    probe = normalize_probe(
        {
            "streams": [
                {"index": 3, "codec_type": "audio"},
                {"index": 1, "codec_type": "audio"},
            ]
        }
    )

    assert [stream.index for stream in probe.audio_streams] == [1, 3]


def test_normalize_attachment() -> None:
    probe = normalize_probe(
        {
            "streams": [
                {
                    "index": 4,
                    "codec_type": "attachment",
                    "codec_name": "ttf",
                    "tags": {
                        "filename": "Example.ttf",
                        "mimetype": "application/x-truetype-font",
                    },
                }
            ]
        }
    )

    assert probe.attachments[0].filename == "Example.ttf"


def test_normalize_chapters() -> None:
    probe = normalize_probe(
        {
            "chapters": [
                {
                    "id": 1,
                    "start_time": "300.0",
                    "end_time": "600.0",
                    "tags": {"title": "Chapter 2"},
                }
            ]
        }
    )

    assert probe.chapters[0].title == "Chapter 2"


def test_missing_sections_produce_empty_collections() -> None:
    probe = normalize_probe({})

    assert probe.video_streams == []
    assert probe.chapters == []


def test_unknown_stream_types_are_ignored() -> None:
    probe = normalize_probe({"streams": [{"codec_type": "data"}]})

    assert probe == NormalizedProbe()


def test_recognized_stream_without_index_fails() -> None:
    with pytest.raises(ProbeNormalizationError):
        normalize_probe({"streams": [{"codec_type": "video"}]})


def test_format_probe_summary_is_stable() -> None:
    summary = format_probe_summary(
        path=Path("/media/movie.mkv"),
        normalized=NormalizedProbe(
            container="matroska,webm",
            duration_seconds=600.0,
            bitrate_bps=8123456,
            video_streams=[
                VideoStream(
                    index=0,
                    codec="hevc",
                    width=3840,
                    height=2160,
                    fps="24000/1001",
                    bit_depth=10,
                    pix_fmt="yuv420p10le",
                    color_space="bt2020nc",
                    color_primaries="bt2020",
                    color_transfer="smpte2084",
                    hdr_metadata_present=True,
                )
            ],
            audio_streams=[
                AudioStream(index=1, codec="eac3", language="eng", channels=6, default=True)
            ],
            subtitle_streams=[SubtitleStream(index=3, codec="subrip", language="eng", forced=True)],
        ),
        probe_hash="hash",
    )

    assert summary == "\n".join(
        [
            "File: /media/movie.mkv",
            "Container: matroska,webm",
            "Duration: 600 s",
            "Bitrate: 8123456 bps",
            "",
            "Video streams:",
            "  [0] hevc 3840x2160 24000/1001 fps 10-bit yuv420p10le",
            "      color=bt2020nc/bt2020/smpte2084 hdr_metadata=yes",
            "",
            "Audio streams:",
            "  [1] eac3 eng 6 channels default",
            "",
            "Subtitle streams:",
            "  [3] subrip eng forced",
            "",
            "Attachments: 0",
            "Chapters: 0",
            "Probe hash: hash",
        ]
    )
