from __future__ import annotations

from typing import Any


def representative_probe_payload(*, duration: str = "600.000000") -> dict[str, Any]:
    return {
        "streams": [
            {
                "index": 0,
                "codec_name": "hevc",
                "codec_type": "video",
                "width": 3840,
                "height": 2160,
                "pix_fmt": "yuv420p10le",
                "avg_frame_rate": "24000/1001",
                "r_frame_rate": "24000/1001",
                "bits_per_raw_sample": "10",
                "color_space": "bt2020nc",
                "color_transfer": "smpte2084",
                "color_primaries": "bt2020",
                "side_data_list": [
                    {"side_data_type": "Mastering display metadata"},
                    {"side_data_type": "Content light level metadata"},
                ],
            },
            {
                "index": 1,
                "codec_name": "eac3",
                "codec_type": "audio",
                "channels": 6,
                "tags": {"language": "eng", "title": "Main Audio"},
                "disposition": {"default": 1, "forced": 0},
            },
            {
                "index": 2,
                "codec_name": "aac",
                "codec_type": "audio",
                "channels": 2,
                "tags": {"language": "eng", "title": "Director Commentary"},
                "disposition": {"default": 0, "forced": 0},
            },
            {
                "index": 3,
                "codec_name": "subrip",
                "codec_type": "subtitle",
                "tags": {"language": "eng", "title": "Forced"},
                "disposition": {"default": 0, "forced": 1},
            },
            {
                "index": 4,
                "codec_name": "ttf",
                "codec_type": "attachment",
                "tags": {
                    "filename": "Example.ttf",
                    "mimetype": "application/x-truetype-font",
                },
            },
        ],
        "chapters": [
            {
                "id": 0,
                "start_time": "0.000000",
                "end_time": "300.000000",
                "tags": {"title": "Chapter 1"},
            },
            {
                "id": 1,
                "start_time": "300.000000",
                "end_time": "600.000000",
                "tags": {"title": "Chapter 2"},
            },
        ],
        "format": {
            "format_name": "matroska,webm",
            "duration": duration,
            "bit_rate": "8123456",
        },
    }
